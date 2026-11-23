# Multiplayer: members, lenses, the phone gate, the journal, the inbox and agents as players

Written 2026-11-22, from a design conversation with the owner. Phases 1 to 8 are built (each has its Built note). This note is
the brief for building it: what multiplayer means for Photo Estate, the decisions already made (and
why), the data model, and the work in phases, each phase a pull request that can be deployed on its own.
When a phase lands, the book describes the result and this note keeps the reasoning.

Reviewed against the code and the Mac mini on 2026-11-22, before building. What that changed is marked
*(review)* where it is not obvious, and the questions it left for the owner are at the end.

Read [CLAUDE.md](../../CLAUDE.md) first: how to run the app on the demo library, the tests, the rules for
the script modules and for the docs, and how a branch reaches the Mac mini.

## How to build it: fast, and small

**This matters more than anything else in this note.** Build for maximum velocity, and do not
over-engineer. Multiplayer must not bloat the code base.

- Take the smallest version of each piece that does the job for one family on one Mac mini: a table
  and a function before a framework, a plain string before a registry, a line in a runbook before a
  tool. Nothing here needs to scale past a dozen members.
- Reuse what exists: change sets for every review, `agent_tasks` and `agent_events` for agent runs,
  the existing pages and flows. A new module only when no existing one is the home for it.
- Build nothing ahead of need: no options, abstraction layers or endpoints for later phases or
  hypothetical uses. Where this note describes more than a phase needs, build less and say so in the
  pull request.
- Each phase should add little code. If a phase is growing large, stop and cut scope before going on.

## What multiplayer is

Until now there has been one user: the owner. Multiplayer lets the family use the estate, each person
with their own view of it, while the estate stays one family's archive, on one computer, that nobody
outside the family hosts.

Four ideas carry the design:

1. **Identity comes from the tailnet.** Photo Estate never handles a password or an OAuth flow.
2. **Facts are shared; views are personal.** Who is in a photo, when and where it was taken, and the
   stories told about it belong to the estate. What a person hides, favourites and curates is theirs.
3. **The estate is append-only.** Nothing the app, an agent or a phone does ever deletes a photo or its
   history. Only the sysadmin can purge, by hand.
4. **Agents are players.** They are members with an identity, act for someone, propose rather than
   impose, and ask people for the stories that only people know.

## Decisions, and why

| Decision | Why |
|---|---|
| Identity is the `Tailscale-User-Login` header that `tailscale serve` adds; the app keeps its own member table that logins map to | Tailscale already signs people in with Apple, Google or Microsoft. The provider proves *who* someone is; the estate decides *what they may do*. No photo goes through the provider. A person who changes provider, or an heir who takes over, is a change of mapping, not of history |
| No general access control (who may see what) for now | A lot of complexity for little gain. The one exception is *private to the uploader*, below |
| Curation is **lenses**: named views, personal by default, that can be shared. The estate's own curation is lenses every member is subscribed to by default | One person's hidden photos are not everyone's. "Global" curation becomes one more lens, not a separate state |
| One state, **private to the uploader**, entered two ways: *held* by ingestion (before anyone saw it) or *withdrawn* by the uploader (after sharing) | The receipt for a Christmas present has to be private before anyone sees it; withdrawing is the fallback for what the classifier misses. Both behave the same, so there is one rule to enforce |
| Ingestion holds receipts, screenshots and documents | The cheap, obvious categories. Each hold is a journal entry and an inbox item, so a person reviews it |
| The **phone gate**: a member may review each batch on the iPhone before it is uploaded. An opt-out stands until that member takes it back, on their phone | Some family members will not want to share everything. A photo opted out on the phone never reaches the mini, so no backup, agent or person can see it |
| Opt-outs are kept as content hashes (tombstones), on the phone and mirrored to the mini | A tombstone keyed by the asset's identifier would be lost when the phone is replaced, and the next sync would upload everything that had been opted out. An opt-out is the member's own: it stops *their* copy, not another member's copy of the same photo |
| **Append-only**: hiding, withdrawing, every change to a lens or album is an entry in the journal; undoing is another entry. Only the sysadmin purges, by hand, outside the app | Avoiding accidental deletion matters more than almost anything else. Members are told before they share that a shared photo can be withdrawn from view but not erased |
| A **journal**: one append-only log of everything that happens, each entry with its actor | It gives listeners something to react to, explains every hidden photo ("hidden by Mom's *No screenshots* lens"), and is what the knowledge estate will read |
| An **inbox** per member: everything that needs a person's decision goes there | Every "propose" in this design ends in someone deciding. The in-app inbox is enough for now: no push notifications |
| A **sysadmin** role (the owner, for now) | Someone approves new members, decides the estate's default lenses and holds the purge. Succession is a later plan; the role is a row, not code, so it can be handed on |
| Agents: the **estate's agent** runs on the mini on the owner's subscription and does family work. A **personal agent** runs in the member's own harness, on their own account, and connects to `/mcp` over the tailnet | Clean from the beginning: the owner pays for family work; whoever wants a personal agent brings and pays for it |
| An agent acts for someone: it can see only what that member can see, and writes only to what that member owns | Otherwise an agent can be used to reach what its user cannot |
| New photos go straight into a series, with no proposal step | A sensible start. More review can be added after some use |
| Answers to an agent's questions can be spoken; the recording is kept in the estate, transcribed on the mini with NVIDIA Parakeet TDT 0.6B v2 (what FluidVoice uses) | For an oral history the voice is worth more than the transcript. It need not be real time |
| New photos for a member's own series go to *that member's agent*, which picks the request up when it connects; the estate's agent keeps only family series going. Every estate-agent run, including the implicit ones, is recorded | The member's agent may know things the estate's agent does not. The records let spending be read off later |

## Words

These are new words in the code and the book. Some old ones are taken, so mind the collisions:

- **member**: a person or an agent the estate knows (later a device, such as a photo frame). Not
  `persons`, which are the people *in* photos (faces.py). A member may be linked to a person.
- **login**: a Tailscale login (`alice@icloud.com`), mapped to one member. A member may have several.
- **role**: `sysadmin`, `member` or `viewer` (`viewer` is for devices later; nothing uses it yet). More
  than one member may be a sysadmin.
- **sharer**: a member who uploaded a photo. A photo the whole family photographed or forwarded may have
  several. Ingestion merges copies by content hash, so "who uploaded it" is a set, not one member.
- **private**: a photo that is private to a sharer: only that sharer sees it. Reached by *held*
  (ingestion) or *withdrawn* (the sharer). Leaving it is *released*: by the sharer, or by itself on a
  date the sharer set ("private until Dec 26").
- **lens**: a named view. A *filter* lens leaves photos out; a *series* picks and orders photos
  (an album). Live (follows a search, or is kept up to date by an agent) or a snapshot. Private or
  published. An **estate default** is a published filter lens every member is subscribed to unless they
  opt out. The two kinds share everything (one table, one mechanism) except how they are used:
  - a **filter is always on**: once subscribed, it changes what the member sees everywhere (search, the
    grid, People, their phone), and the filters a member subscribes to add their hides together;
  - a **series is opened**: a place to go and look, which changes nothing anywhere else, and is looked
    at one at a time. Subscribing to a series puts it in the member's Albums and tells them when it
    grows. A series is the existing albums, made live; the filter is the genuinely new kind.

  *(review)* How each sits on `changes.py`: a series is one album (`lenses.set_id`). A filter is every
  hiding change set applied to it (`change_sets.lens_id`): each reviewed batch of hides stays its own
  set, applied and reverted on its own, as today. The owner has made several hiding sets already, and
  one lens cannot be one set.
- **journal**: the append-only log. Not `events`: that name is taken three times already (the catalog's
  photo clusters in places.py, the setup page's job log in intake.db, and the pipeline protocol in
  `core/events.py`). Not the face labels' file either: `face-labels.jsonl`, which faces.py calls its
  journal, is People's own undo log and stays as it is.
- **inbox**: a member's list of things to decide. In code, `inbox_items`. The word "inbox" is also the
  upload folder (`--inbox`, and the sweep that ingests it: `upload.IngestJob`, started by `photo_app.py`); leave that alone and name the new code
  `member_inbox.py` and the table `inbox_items`, so the two are never confused.
- **tombstone**: the content hash of a photo a member opted out of (refused from that member only, until
  they take it back), or one the sysadmin purged (refused from everyone, for good).
- **series**: a lens that picks and orders photos and keeps growing: "Maya learning to walk", "my
  skiing over the years". New photos are added to a series; they never change a story. The two meet
  in three ways: a series shows the stories told about its photos, a gap in a series (photos nobody
  has described) becomes a question whose answer is a story, and a story can start a series.
- **story**: something a member said about one or more photos, attributed and dated: text, or a recording
  and its transcript. Several stories about the same moment are kept side by side; they are not
  corrections of each other.
- **question**: what an agent asks a member (or the whole family) about photos, waiting in the inbox.
- **acting for**: the member an agent's run works for. Every journal entry an agent writes names both.

## Where it starts (the code today)

- `photo_app.py`: `who(headers)` reads `Tailscale-User-Login`; `writer()` checks `--upload-user`;
  `ROUTE_AUTH` and `http_util.Auth` (`PUBLIC`, `WRITER`, `LOCAL_OR_WRITER`) say who may make each
  request. A request from the mini itself may always write. With no `--upload-user`, *anyone who can
  reach the app may write*. Multiplayer ends that.
- `changes.py`: albums and change sets are one table (`change_sets`, `op` `view` or `hide`, `kind`
  `list` or `live` with a `query`). Hiding is global: `hidden(sha256, set_id)`, and a photo is hidden
  while any row names it. Change sets are proposed, reviewed (marks are exceptions), applied, reverted
  and committed. That flow is the model for every review in this plan.
- `catalog.py`: one in-memory `Catalog` with one `visible` mask (not in any hiding set).
- `favorites` table: one row per photo, `set_by` a login. `assets.favorited` comes from the sources.
- `sync.py`: one change feed for every phone. A hidden photo leaves every phone.
- `upload.py` and `admin.py`: the phone `PUT`s originals to `--inbox` as
  `YYYY/MM/DD/<name>-<id>.<ext>`; `ingest` is the only writer of the catalog.
- `ios/`: an iOS 27 `PHBackgroundResourceUploadJobExtension` queues every photo taken after its first run;
  `ForegroundUploader` sends older ones from "Upload recent". Nothing is reviewed before it is sent.
- `tasks.py`: the estate's agent is Claude Code run headless with `--tools ""` (no shell, no files),
  only the photo-estate MCP tools, in an empty temporary folder. Its MCP config carries
  `X-Estate-Task: <task id>`, honoured only from the mini. A guessable number, trusted because it is
  local. The agent is in effect the owner.
- `estate_mcp.py`: the tools. They never return pixels, only ids, scores, dates, places and names.
- Deletes today: `changes.py` (hiding rows, items, discarded sets), `faces.py` (labels, merged persons),
  derived tables rebuilt by `estate.py`, `places.py` and `embed.py`. Nothing deletes from `assets`,
  `occurrences` or the store. `estate.py ingest --delete-after` deletes a *source* zip, not the estate.
- Schema rules (`core/schema.py`): additive only, `ensure()` on open, and
  `tests/fixtures/schema_baseline.json` pins the DDL. Every table below is new or gains columns; nothing
  existing is renamed or dropped.

Found in the review (2026-11-22), and each one changes something below:

- **Pixels are not behind `visible`.** `/thumb`, `/preview`, `/large`, `/original` and `/video`
  (images.py) answer for any sha. Fine for a hidden photo; not for a private one.
- **`visible` is baked into the catalog at load,** not only read by routes: stacks are grouped over
  visible photos (`stack_of`, `dup`), trip cards and their covers are counted over them, `info` counts
  them, and `browse.remember` caches rankings by catalog generation alone. `fav` is in each photo's row
  (`meta`), so it is in the sync feed's digest.
- **People reads `hidden` itself** (faces.py `load`), not `Catalog.visible`, and builds its clusters once
  for everyone.
- **`is_local`** (intake/server.py) is "from 127.0.0.1 with no proxy header". It stays the test for `LOCAL`.
- **The upload already takes a hash**: `X-Content-SHA256`, checked against the bytes (admin.py,
  `upload.save_upload`). The system uploader's jobs do not send it: the extension builds a request for
  a file it never reads.
- **The mini today**: one tailnet user (the owner; the plist names him as the one `--upload-user`), one
  tagged device, and port 8766 served tailnet-only. So every existing photo and album is the owner's,
  and nothing is yet known about what a second user's requests carry. The estate disk is APFS with
  ownership ignored; no `ffmpeg` on the app's PATH.

## The data model

New tables in the catalog. The SQL shows the shape; the implementing phase settles names and indexes
within the schema rules.

```sql
-- who
CREATE TABLE members(
  id INTEGER PRIMARY KEY, name TEXT, kind TEXT,          -- person | agent | device
  role TEXT,                                             -- sysadmin | member | viewer
  person_id INT,                                         -- persons.id: who this member is in photos, if known
  upload_policy TEXT DEFAULT 'review',                   -- everything | review | review_flagged
  joined_at TEXT, status TEXT);                          -- pending | active | revoked (never deleted)
CREATE TABLE member_logins(login TEXT PRIMARY KEY, member_id INT, added_at TEXT);
CREATE TABLE agent_bindings(                             -- a personal agent a member connected
  id INTEGER PRIMARY KEY, member_id INT, device TEXT,    -- device: the tailnet node it came from
  status TEXT, requested_at TEXT, decided_at TEXT);      -- pending | approved | revoked

-- what happened (append-only)
CREATE TABLE journal(
  id INTEGER PRIMARY KEY, at TEXT,
  actor INT,                                             -- members.id
  acting_for INT,                                        -- members.id when the actor is an agent
  kind TEXT,                                             -- photo.shared, photo.held, lens.subscribed, ...
  subject TEXT,                                          -- what it is about: a sha, lens:<id>, album:<id> ...
  data TEXT);                                            -- JSON: whatever else the kind needs

-- whose photos, and which are private
CREATE TABLE sharers(sha256 TEXT, member_id INT, shared_at TEXT, PRIMARY KEY(sha256, member_id));
CREATE TABLE private(                                    -- the photo is private to this sharer
  sha256 TEXT, member_id INT, why TEXT,                  -- held | withdrawn
  since TEXT, until TEXT,                                -- until: released by itself after this date
  PRIMARY KEY(sha256, member_id));
CREATE TABLE tombstones(                                 -- why: opted_out (that member's) | purged (member 0: everyone's)
  sha256 TEXT, member_id INT, why TEXT, at TEXT,
  lifted_at TEXT,                                        -- an opt-out taken back (a purge never is)
  PRIMARY KEY(sha256, member_id));

-- views
CREATE TABLE lenses(
  id INTEGER PRIMARY KEY, owner INT, title TEXT,
  kind TEXT,                                             -- filter | series
  set_id INT,                                            -- a series: the album (change_sets.id) holding its photos
                                                         --   and query. NULL for a filter: its sets name it
                                                         --   (change_sets.lens_id)
  published INT DEFAULT 0, estate_default INT DEFAULT 0,
  maintained_by INT,                                     -- an agent member that keeps it up to date, or NULL
  created_at TEXT);
CREATE TABLE subscriptions(member_id INT, lens_id INT, mode TEXT, at TEXT,   -- live | snapshot | opted_out
  PRIMARY KEY(member_id, lens_id));
CREATE TABLE shown(sha256 TEXT, member_id INT, at TEXT, PRIMARY KEY(sha256, member_id));  -- "show it anyway"

-- deciding and asking
CREATE TABLE inbox_items(
  id INTEGER PRIMARY KEY, member_id INT, kind TEXT,      -- held | review | lens_proposed | lens_changed |
                                                         --   binding | question | series_update | ...
  ref TEXT, title TEXT, created_at TEXT, state TEXT, done_at TEXT);   -- open | done | dismissed
CREATE TABLE questions(
  id INTEGER PRIMARY KEY, asked_by INT, acting_for INT,
  to_member INT,                                         -- NULL: open to the family
  shas TEXT, text TEXT, why TEXT, group_key TEXT,        -- group_key: ask about one event together
  priority REAL, status TEXT, created_at TEXT);          -- queued | asked | answered | closed
CREATE TABLE stories(
  id INTEGER PRIMARY KEY, member_id INT, question_id INT, shas TEXT,
  text TEXT, recording TEXT,                             -- recording: the sha256 of the audio, a file in the
                                                         --   store with no assets row (it is in no photo view)
  told_at TEXT);
CREATE TABLE listener_cursors(name TEXT PRIMARY KEY, journal_id INT);
```

Favourites become personal through a new table, because `favorites` is keyed by the photo alone and a
primary key cannot change additively: `member_favorites(sha256, member_id, fav, set_at)`. The existing
rows are copied into it as the sysadmin's, and `favorites` is no longer written. What a source said
(`assets.favorited`, the phone's `X-Favorite`) counts for the photo's sharers only. Existing tables gain
columns: `change_sets.owner` and `change_sets.lens_id` (the filter a hiding set is applied to), and
`agent_tasks.acting_for`. Old rows belong to the sysadmin. An album needs only its owner; it gets a
`lenses` row when it is first published, because a subscription needs something to name.

### What a member sees

For member *M*, a photo is visible when all of these hold:

1. it is shared: *M* is one of its sharers, or some sharer has it and has not made it private
   (a photo shared by anyone is shared: my opt-out decides my copy, not yours);
2. no filter lens *M* is subscribed to (live, or the snapshot taken) hides it, unless *M* has said
   "show it anyway" (`shown`);
3. it is not a paired Live Photo clip, as today.

The same rules hold inside a series, whoever made it. A photo *M*'s filters hide stays hidden when it
is in Mom's series of the grandchildren, and a series published by someone else never shows *M* a photo
that is private to anyone but *M*. A series view lists its photos through `visible_for(M)` like every
other view; it never reads the album's photos unfiltered.

*(review)* That is two masks, and the code needs both:

- `shared_for(M)`: rules 1 and 3. What *M* may ever be shown. Everything that shows a photo *M*'s
  filters hide goes by this one: the review page of a hiding set, "show hidden", "show it anyway",
  the `why` line, and **every pixel route** (`/thumb`, `/preview`, `/large`, `/original`, `/video`, the
  zip download, face crops), which today check nothing.
- `visible_for(M)`: `shared_for(M)` less what *M*'s filters hide. Every view.

`visible_for` returns a small **member view**, not a bare mask, because the catalog bakes visibility
into more than one array: the mask, and what is derived from it per member (which photo of a stack is
on top and how many it holds, the trip cards and their covers, the counts in `info`, the favourites
array). All of it is a few numpy passes over arrays the catalog already has. `browse.remember` adds the
member to its key. `meta[sha]["fav"]` stops being read by routes: a row's `favorite` is filled from the
member's array as the row is sent.

People is the exception, kept deliberately small: its clusters are built once, over photos that are
private to nobody and that no estate default hides. A member's own filters apply when a person's photos
are listed, not to the clusters. A private photo's faces are in People for no one, its sharer included,
until it is released.

`Catalog` keeps the photo arrays it has today. What changes is that `visible` becomes a mask per member,
worked out once and cached until a journal entry that touches visibility arrives. A mask is one bit per
photo, so 100k photos for a dozen members is about 150 KB. Every route that reads `visible` (search,
facets, Years and Months, trips, People, `/api/info`, the sync feed, the MCP tools) reads the mask of
the member the request is for. Getting this right everywhere is the riskiest part of the work, so phase
3 starts by sending every reader of `visible` through one function and testing that function.

The sync feed becomes one feed per member (`sync-feed-<member id>.db` beside the catalog), so a photo
that one member's lens hides leaves only that member's phones. The first sysadmin keeps the existing
`sync-feed.db`, so the owner's phone does not fetch the library again; a member's file is made the
first time one of their phones asks, not at start.

### What is shared, and what is personal

| Shared (facts of the estate, every change journaled with its actor) | Personal (the member's own) |
|---|---|
| The photos and their metadata, places, events, trips | Filter lenses and what they hide; "show it anyway" |
| People: naming faces, merging persons | Favourites |
| Stories, recordings, answers | Albums and series (until published) |
| Published lenses and estate defaults | Subscriptions |
| | Private photos, their holds and releases |

## Append-only

- **Journal first.** Every write in this plan appends its journal entry in the same transaction as the
  state it changes. State tables (`hidden`, `subscriptions`, `shown`, `private`...) may still be updated
  and have rows deleted: they are the *current* state, and the journal says how it got there. Undo is a
  new entry that reverses the state, never a deleted entry.
- **Protected tables.** SQLite triggers raise on `DELETE` from `journal`, `assets`, `occurrences`,
  `sharers`, `tombstones`, `stories`, `members` and `member_logins` (a member is revoked, never
  deleted), on any `UPDATE` of `journal`, and on an `UPDATE` that changes a key (`sha256`, an id) of the
  others. *(review)* The other columns of `assets` and `occurrences` stay updatable: ingest and embed
  correct dates, places, sizes and sidecar links today (estate.py, embed.py) and must go on doing so.
  Nothing in the code deletes from these tables today, so no caller changes. The triggers are new rows
  in `sqlite_master`: `schema_baseline.json` gains them. The purge tool (below) is the one thing that
  drops a trigger, inside its own transaction.
- **Albums are not deleted.** `changes.py`'s discard marks a set `discarded` and journals it rather than
  deleting its rows. The face labels' undo in `faces.py` stays as it is (it is shared state), and is
  journaled.
- **Immutable originals.** Ingestion sets the user-immutable flag (`chflags uchg`) on each file it writes
  to the store, so even `rm -rf` in the wrong folder fails. The files already in the store get it from
  one `chflags` command run once on the mini, after a backup. *(review)* Never in `--demo` or on the
  checked-in demo store (git must be able to change it). The estate disk ignores ownership, so any
  user of the mini can clear the flag: it guards against accidents, which is its job, not against
  intent. Try it on one scratch file on the volume before the whole store, and tell the backup plan:
  a copy that keeps the flag (rsync `--fileflags`) cannot be pruned without clearing it.
- **Deletions upstream never propagate.** A photo deleted on an iPhone, in iCloud or in the OneDrive
  mirror stays in the estate. Sources only add. Write a test for each source that removes a file and
  re-ingests, and checks the catalog and the store are unchanged.
- **Purge.** `estate.py purge <sha> --reason "..."`: run by hand on the mini by the sysadmin, never from a
  page, a phone or an agent. It takes a catalog backup first (`.backup`, then `pragma quick_check` on the
  copy), appends a `photo.purged` journal entry (who, when, why: the fact survives the bytes), adds a
  tombstone for everyone (member 0) so no source brings the photo back, clears the immutable flag and removes the file. It says
  that the photo remains in catalog backups until they rotate out.
- **Members are told.** The phone gate's screen says, before the first share: shared photos become part
  of the family estate; you can withdraw one from view later, but not erase it.

## Phases

Each phase is a pull request, with its tests, its docs (the book chapter it changes, appendix B for each
endpoint, chapter 2's file list, the glossary), and a deploy of its branch to the mini as CLAUDE.md
describes. Every phase that touches the catalog's schema takes the backup CLAUDE.md gives first. Phases
1 to 6 are built in that order, and 7 and 8 need 4. *(review)* The inbox was phase 6 and is now phase 4,
ahead of private photos and the phone gate: both put their decisions in it, and a held photo nobody is
told about is a lost photo.

### Phase 0: check two things first (a day, no code)

1. **The identity header for every way in.** Tailscale offers two ways to let family reach the mini:
   inviting them as users of the owner's tailnet, or *sharing* the mini with their own tailnets. Check
   on the mini with a second account that `tailscale serve` sets `Tailscale-User-Login` for both, and
   what it sets for the owner's own tagged devices (if any). The plan assumes invitations, with a
   `group:family` in the tailnet's default-deny policy granting only the app's port, and needs the
   header to be there. Check the plan's user limit too. The tailnet has one user today, so this needs
   a second account (a family member's, or a spare of the owner's with another provider): it cannot be
   checked from the owner's devices. A tagged device is expected to carry no login at all; it is then
   not a member, which is right until devices are members.

   *Checked 2026-11-22, for an invited user:* a second account of the owner's was invited into the
   tailnet and signed in on a second, userspace `tailscaled` on the laptop (the test member's node,
   `estate-tester`; its proxy is `localhost:1055`). Through it the mini answers on 8766 and on no other
   port, so the `group:family` grant holds, and `tailscale whois` on the mini names the node's user by
   its login. `tailscale serve` takes its header from the same lookup, but the header itself has not
   been read yet: phase 1's request-access page will show it. Still to check: a node *shared* from
   another tailnet, only if family will not be invited; and the plan's user limit.
2. **Where personal agents can run.** A personal agent reaches `/mcp` only from a device on the tailnet
   (Claude Code; a desktop app with a local MCP configuration). Connectors added in claude.ai or the
   Claude iPhone app are expected to be called from Anthropic's servers, which are not on the tailnet.
   Confirm, and write the answer into the "Connect your agent" page. The tailnet stays closed: no
   Tailscale Funnel, no public `/mcp`.

Not checked, on purpose: whether the owner's Claude subscription may run the estate's agent for the
family. While this is being proved out the tokens are few; leave `tasks.py` as it is.

### Phase 1: members and identity

*Built 2026-11-22 (branch `multiplayer`); [chapter 11](../book/11-server.md#members-and-who-may-do-what)
describes it. Built smaller than this list: the roles are `sysadmin` and `member` (no `viewer` until
something uses it); no inbox item for a request (phase 4); old `created_by`/`set_by` values are logins
and are left as they are, since a login already names its member; `/api/info` has no version to show, so
a non-member is told only `{"member": false}`.*

- The tables `members`, `member_logins` and `journal`, and the module `members.py` (with its unit test):
  look a login up, add a member, change a role or status. The journal table lands here so member
  changes are journaled from the first day; phase 2 journals everything else and adds the protections.
- `who(headers)` returns a member, not a string. A login with no member gets **a request-access page**:
  "Ask John to let you in", which makes a `pending` member and an inbox item for the sysadmin (the page
  can be plain until phase 4; until then the sysadmin approves on `/members`).
- Roles replace `--upload-user`. On first start with the new code, the catalog has no members: the
  logins given by `--upload-user` become active members, the first of them the sysadmin (or
  `--sysadmin <login>`, a new option). Old `created_by`/`set_by` logins become those members.
- `Auth` becomes role-based: `MEMBER` (an active member), `SYSADMIN`, and `LOCAL` (a request from the
  mini itself, kept for the CLI, the tools and the tests, and treated as the sysadmin). The default flips
  from "anyone who can reach the app may write" to "only members may do anything at all"; only the
  request-access page, the home-screen app files and `/api/info`'s version are open.
- `/mcp` follows the same rule as every route: a login is that member, a local call is the sysadmin.
  The estate's agent stays local with `X-Estate-Task` until phase 7 gives it a token. *(review)* A task
  records who submitted it (`agent_tasks.acting_for`) from this phase, so phase 3 can use it.
- On the mini nothing changes for the owner: the plist's one `--upload-user` becomes the sysadmin.
  Leave the option in the plist until this phase has started once there.
- `/members` (sysadmin): who is in, their logins, roles, upload policies; approve, revoke, link a member
  to a person in People.
- `GET /api/me`: the member, role, upload policy. The iPhone app reads it.
- **Demo and preview.** `--demo` makes three members (a sysadmin and two members) and accepts
  `X-Demo-Member: <name>`, or a `demo_member` cookie, in place of the Tailscale header, *only* with
  `--demo`, so Playwright tests and the cloud preview can be each of them. *(review)* The cookie is for
  a person trying the preview: a browser cannot add a header, so the demo's `/members` has a "be this
  member" link that sets it. With neither, a demo caller is the demo sysadmin (the preview is reached
  through `tailscale serve`, with a real login that is no demo member). Never honour either without
  `--demo`.

### Phase 2: the journal and append-only

*Built 2026-11-22 (branch `multiplayer`); [chapter 4](../book/04-store-and-ingest.md#append-only-sources-only-add),
[chapter 5](../book/05-catalog.md#protected-tables) and [chapter 11](../book/11-server.md#the-journal)
describe it. As listed, with these choices: the actor reaches each write as the member id beside the
login the code already passed (`m.id` from `who()`; a request from the mini is the sysadmin), so a task's
agent writes as the sysadmin until phase 3 reads `agent_tasks.acting_for`, and a run's own end is
journaled with no actor, acting for whoever asked; a write that changes nothing appends nothing. The
immutable flag is set by ingest only in a store whose root holds a `.immutable` file, made by the
one-off step in MAC-MINI.md, so scratch, test, practice and demo stores are never flagged and can be
deleted as before. The `tombstones` table is made now (the purge needs it), and ingest refuses a purged
hash; the purge also removes what is worked out from the photo (vectors, faces, places, its text index
row, album items) and, with `--cache`, its cached copies, and lists the sources that still have it. The
migration is `estate.py migrate` and does only the sharers half: the estate-default half comes with
lenses (phase 3), in the same command. The test of sources losing a file is one per kind (a Takeout
zip, an iCloud folder, the OneDrive mirror, the phone's inbox). Beyond the plan's triggers, found in review:
`BEFORE INSERT` triggers stop an `INSERT OR REPLACE` from deleting a protected row (refused for the
journal and members, skipped for the rest), and a purge's tombstone can never be lifted. Not built: no
endpoint, and nothing reads the journal.*

This phase is the easiest to over-build. Keep it to this list, each item as small as it can be:

- `journal.py` (unit-tested): two functions, `append(con, actor, kind, subject, data, acting_for=None)`
  in the caller's transaction, and `since(id)` for listeners. Kinds are plain strings, listed in the
  module's docstring; no registry, no classes, no event-sourcing. State tables stay the truth for what
  the app shows; the journal is the history beside them.
- One `append` call at each write that exists today: change sets and albums, favourites, People, tasks,
  ingest finishing. One entry per action, not per photo: applying a set that hides 10,000 photos is one
  entry naming the set. People keeps `face-labels.jsonl`; its journal entry is a line beside that, not
  a replacement. No new endpoint: nothing reads the journal until phase 7's listeners.
- The protected-table triggers, and discard-as-mark in `changes.py`.
- The immutable flag: set at ingest; for the files already in the store, one `chflags` command in
  MAC-MINI.md, run once after a backup, not a tool.
- The purge: one small `estate.py purge` command, as described in Append-only.
- One test that a source losing a file changes nothing in the estate.
- On the mini: the migration that gives every existing photo the sysadmin as its sharer (shared), and
  makes the existing hiding sets the first estate default (see phase 3), runs after a backup. So
  `sharers` is made in this phase. The migration is a command run once (an `estate.py` subcommand), not
  part of `ensure()`, which must not scan a table.

### Phase 3: lenses and what each member sees

*Built 2026-11-22, the server half (branch `multiplayer`); [chapter 11](../book/11-server.md#what-each-member-sees-lenses)
describes it, appendix B lists its endpoints; the pages (the Albums tab's lenses, subscribing, the review
page's choice of lens, the viewer's "why" line) come next, on these endpoints. Built in the order this
phase asks: first every reader of `Catalog.visible` and of the baked stacks, trip cards, `info` and `fav`
went through `Live.visible_for` with the same answers as before (the whole suite green), then what it
computes changed. The choices: `visible_for` returns a `catalog.View`, and the `Catalog` no longer has
those fields, so a route that reached for them fails (and tests/test_multiplayer.py reads the route
modules' code to refuse the other ways round it). A photo's facts (`meta`) no longer carry `fav`; a row's
is the member's. Views are kept per member until the catalog or the looks (`lenses.State`, read from the
tables) change; a hide no longer reloads the catalog, only the looks. A snapshot is a copy into a lens or
album of the subscriber's own (no table of its own). Proposing is a column, `lenses.proposed_at`; accept
and decline are `POST /api/lenses/<id>/decide`. The lens a hiding set hides for is set by its first
apply (`{lens: "mine" | "estate" | id}`), and the lens made on first use is recorded in that apply's
journal entry (`album.applied {lens, lens_made}`), so it stays one entry per action. Before `estate.py
migrate`, a set with no lens hides for everyone and an owner-less album, an old `favorites` row and a
photo with no sharer are the first sysadmin's: the owner sees what he saw, so the app works between the
deploy and the migration. A task's writes are journaled with no actor (the agent is no member yet),
acting for whoever submitted it. People: built once without what the estate defaults hide; a member's own
filters drop faces from what People lists to them and pick their covers, but its counts stay People's
and a member who opted out of an estate default still sees none of its faces there. An album lists only
what its viewer sees; a set up for hiding (its owner's review) lists all of its photos, marked. Not
built: no inbox item for a proposal or a lens that changed (phase 4); `maintained_by` is a column
nothing sets; the pixel routes ask `shared_for`, which is every photo until phase 5. The cost:
docs/notes/PERFORMANCE.md (2026-11-22).*

*The pages, the same day ([chapter 26](../book/26-albums-and-agents.md#lenses-on-the-pages)): no new
endpoint. The Albums tab lists the member's albums and subscribed series as cards, then the filters (theirs,
proposals for a sysadmin, estate defaults, those others publish) and others' series as rows with what each
offers, and says what subscribing does in this note's words; **See them** is Search with `hidden=1`. The
review page chooses You or Everyone before a set's first apply and names its lens after; the viewer asks
`why` for each photo that stays and offers "show it anyway"; the Library's menu says who you are and links
`/members` for a sysadmin. The machines and the words are one module, `app/lens-flow.js`, with its unit
test, and `tests/test_lenses_browser.py` uses them as Ada, Ben and Cleo. The server's change: `why`'s text
reads "Hidden by your lens “Hidden photos” on Nov 23", and a member's own filter is titled "Hidden photos"
(whose it is is said beside it). Not built: making a new empty filter from the page (`POST /api/lenses`
has no button: a filter is made by hiding, or by keeping a copy), and any list of the photos shown anyway
(one is taken back from the viewer); a snapshot ends a live
subscription, as the server keeps one mode per member and lens.*

- `lenses.py` (unit-tested): a lens is a row of `lenses` over a change set (`changes.py` keeps doing
  the work of holding photos, sections, reasons and queries). A filter lens's photos are hidden from
  its subscribers: `hidden(sha256, set_id)` stays as it is, and what a member's filters hide is the rows
  of the sets that name those lenses (`change_sets.lens_id`). A series is an album with an owner,
  reusing the album code almost entirely.
- One function, `visible_for(member)`, the only way any route asks what a member sees, built from the
  rules in "What a member sees" above (the member view: the mask and what the catalog derives from it),
  cached per member, cleared by journal entries about visibility. `shared_for(member)` beside it is
  everything today (nothing is private until phase 5), and the pages that show hidden photos read it.
  First move every reader of `Catalog.visible` onto `visible_for` with the same answer as today, then
  change what it computes.
- The existing global hiding becomes the lens **Estate defaults** (owner: the sysadmin, published,
  estate default), so the family first sees the library as the owner curated it, and can opt out.
- Personal hiding: the review page applies a change set to *my lens* (the default) or, for the
  sysadmin, to an estate default.
- Subscriptions: subscribe to a published lens, live or as a snapshot (a snapshot copies its photos into
  a lens of your own); opt out of an estate default; "show it anyway" for one photo. Say on the page
  what subscribing does: to a filter, "this changes what you see everywhere"; to a series, "this adds
  it to your Albums". A live lens that
  changes makes an inbox item for its subscribers in phase 4 (`lens_changed`), and the change applies
  at once.
- **Explain it**: `GET /api/photos/<sha>/why` answers why a member does not see a photo ("hidden by
  Mom's *No screenshots* lens on Nov 3"); the viewer shows it. *(review)* It reads the state tables
  (`hidden.hidden_at`, the set, its lens, the lens's owner), which already hold all of that: no scan of
  the journal.
- **The agent answers for a member** from this phase: the MCP tools read the view of the login that
  called, or for a task, of `agent_tasks.acting_for` (looked up from `X-Estate-Task`). Otherwise a task
  Mom submits searches as the owner. The token of phase 7 replaces the header, not this rule.
- Proposing an estate default: any member can publish a filter lens and propose it; the sysadmin
  decides (an inbox item in phase 4).
- Favourites per member. Albums get an owner and `published`.
- Per-member sync feeds; the iPhone app is unchanged apart from receiving its member's feed.
- The search page's Albums tab shows lenses: mine, published by others (subscribe), estate defaults.

### Phase 4: the inbox

*Built 2026-11-22, the web and server half (branch `multiplayer`); [chapter 11](../book/11-server.md#the-inbox)
describes it, [chapter 26](../book/26-albums-and-agents.md#the-inbox) the page, appendix B its endpoints. One
table (`inbox_items` in the `members` area, so everything that writes the journal has it) and one small
module of functions: `put` (one open item per member, kind and ref, brought up to date rather than repeated)
and `close` (every open item of a kind and ref, or one member's), called in the transaction of what made or
decided the item. The kinds and their triggers: `member_request` from `members.request`, for every active
sysadmin, closed by `members.change` moving the member out of pending (let in or turned away);
`lens_proposed` from `lenses.propose`, for every other active sysadmin, closed by `decide` or by the owner
unpublishing; `review` from `Tasks.move` when a task ends (and a run the app restarted under), for whom it
acted, one per hiding set its agent made that has photos and hides none yet, closed when the set is applied
in full, taken down to an album or discarded: the end of the task and not the set's creation, because an
agent fills a set over many calls; `lens_changed` from changes.py, for the live subscribers of a published
filter whose hides changed (apply, revert, a hidden photo taken out) or a published series whose photos or
search did, never its owner or whoever changed it, one item per member and lens updated in place, closed
by unsubscribing or keeping a copy instead. Estate defaults tell nobody: they have no subscription rows,
and the sysadmin's everyday hiding would reach every member. Only dismissing is journaled
(`inbox.dismissed`): the entry of what made or decided an item already says that. Endpoints: `GET /inbox`,
`GET /api/inbox`, `POST /api/inbox/<id>/dismiss`, nothing else. The page is `inbox.html` with
`app/inbox-flow.js`; the count is on the Library's ⋯ button and its new Inbox row (no tab: the tab bar is
the iPhone app's two places and Search). A lens that changed asks for no decision, so opening it from the
inbox dismisses it. The request-access page says the sysadmin has been told. With it, two cleanups from
phase 3: the viewer's why line says the day in the viewer's time zone (`why.text` stays UTC), and a hiding
set's photos in Albums say "Hidden from you" or "Hidden from everyone". And a test-only fault: a demo
app's tasks worker outlived its scratch catalog at exit (the "disk I/O error" after the summary), so
`Tasks.stop()` now ends it first. Not built: the iPhone app's Inbox tab (phase 6), any kind of a later
phase, a list of done or dismissed items, push notifications.*

- `member_inbox.py` (unit-tested) and `inbox_items`. Items are made by the code that needs the
  decision: `held` (photos held at ingestion, grouped per batch), `review` (a change set to look over),
  `lens_proposed` (for the sysadmin), `lens_changed` (for subscribers), `binding` (phase 8),
  `upload_batch` (the phone's own, phase 6),
  `question` and `series_update` (phase 7), `member_request` (for the sysadmin). Each kind opens the review screen it
  already has, or a new one, as an app of its own (the change set review page is the first).
- `/inbox` on the web, with its state machine in `app/inbox-flow.js` (strict `flow()`, unit test in
  `app/tests/`), and an Inbox tab in the iPhone app with a badge. From phase 6 the phone's own upload
  batches show in the same list, beside the mini's items.
- Each kind is added by the phase that needs it. This phase ships the ones that exist by now:
  `member_request`, `review`, `lens_proposed`, `lens_changed`.
- No push notifications for now.

### Phase 5: sharers, private photos, the hold at ingestion

*Built 2026-11-22 (branch `multiplayer`); [chapter 11](../book/11-server.md#private-photos) describes it,
[chapter 26](../book/26-albums-and-agents.md#private-photos-on-the-pages) the pages, appendix A the commands,
appendix B the endpoint, MAC-MINI.md the by-hand steps. One module, `private.py`, with the rule (`Private`, read
into `lenses.State`, so `shared_for` changes there and nowhere else), the sharer ingest records, the writes, the
hold and the one-off run. The choices:*

- *Who shared it: uploads land in `<inbox>/m<member id>/...` (admin.py, from the member the request is for; the
  first sysadmin's file already at the inbox's top is found there, so an old phone's retry is a `200 exists`),
  and `estate.py ingest --uploads` (ingest-iphone.sh passes it) reads the member back; everything else, and a
  folder of no member's, is the first sysadmin's. A photo that had no sharer row gets the first sysadmin's first,
  so a second member's copy never takes the photo from its owner. Before this phase ingest wrote no sharer at
  all (only `migrate` did); now it writes one for every file it reads. A member folder is no album.*
- *`private` gained two columns beyond the plan's: `released_at` (a released row stays, so the one-off run never
  holds again what a member let go) and `batch` (the `photo.held` entry that held it: its inbox item is
  `held:<batch>`, done once none of the batch is still held). And a third state beside held and withdrawn:
  **`new`**, a phone upload not checked yet.*
- *Where the hold runs, and why it cannot race: ingest writes the `new` row in the transaction that adds the
  photo, so no catalog, loaded at any moment, holds a new upload without it, and the app reads who is private
  after it reads a catalog, inside the swap that installs it (and at startup after the catalog). The check is
  then a step of ingest-iphone.sh after the embeddings and before `catalog_ready` (`private.py check`): it holds
  or lets go each `new` row that has a vector; one with none waits, in no view meanwhile. So the order of the
  script is not what keeps a photo private, the row is: a reload by another job, or a restart, between ingest and
  the check shows the photo to its sharer only. Not chosen: scoring in the app before it installs a catalog,
  because a restart, a setup job's reload or a second process would each need it too.*
- *What is held: images of phone uploads (videos are not scored), from every member, the owner too, that nobody
  else is shown yet: a new photo, or one every sharer holds private (a second member's copy of a held receipt is
  held for them too). Not bulk imports, and not a photo the family already sees.*
- *The prompts' text vectors are a checked-in file, `hold-prompts.json`, made by `embed.py hold-prompts`: the
  check needs numpy and the catalog, never the model or the app. Without them it stops and the photos stay
  private.*
- *Prompts and threshold (`private.PROMPTS`, `HOLD_AT`, in one place): "a receipt", "a screenshot of a phone",
  "a document", each a plain cosine with the photo's vector, held at 0.075 or over on any, or by
  `catalog.is_screenshot`. On the demo's 104 photos the highest score of any prompt is 0.058 (it holds none);
  receipts, phone screenshots and documents made with Pillow (a test in the model's environment makes and scores
  some) score 0.086-0.17 on their prompt. 0.075 sits below the search's own floor (0.085), leaning towards
  holding. Not yet checked on a sample of the real library: that is the dry run's job (MAC-MINI.md).*
- *A photo private to someone else does not exist for a member: every route answers it as a sha it does not have
  (the pixel routes, `why`, similar, a stack, the MCP tools, favourites, adding it to an album). Found while doing
  it and fixed, beyond the routes listed: an event's or trip's title, count and dates in a search's scope; a
  person's name or a place read from a typed search (and the MCP's `people`); a hiding set's covers, counts and
  review page; a filter lens's count (and the inbox's `lens_changed` title, counted without any private photo);
  a snapshot of a filter; a live album's search checked as the sysadmin instead of its owner; a Live Photo's
  clip (served for any sha before: now whoever may see one of its stills); a face crop; a task's events and
  report, which any member could read (now its member's only); `/stats` and `GET /api/ingest`, which counted
  and named every file (now the sysadmin's; `/stats`'s library figures leave other members' private photos out).*
- *The estate's agent acting for no member reads as member 0, `NOBODY`: only photos private to nobody.*
- *Release on a day is by the computer's own date (the family's time zone), checked every minute by a thread
  `start()` starts; its entry has no actor, acting for the sharer. The held view is a search (`is:held`), with a
  bar that releases all of them or those chosen (a tile tapped is chosen while choosing): the pages had no
  selection to reuse; the review page's marks were the nearest, but a change set is the wrong thing to hold
  private photos in.*
- *Not built: the phone's screenshot header and withdrawing from the phone (phase 6); scoring videos; a list
  of what was released. Known and left: places.py works an event's place out from all its photos, so a shared
  photo can carry a place (or an event title) inferred from a private one of the same event; `/compare`'s runs
  and reports are shared by every member and keep the ids the runner's searches found; the local tools
  (`estate.py review`, `faces.py serve`, `embed.py search`) read the whole catalog, as before.*

- **Who shared it.** Uploads land in `<inbox>/m<member id>/YYYY/MM/DD/...` (the upload path keeps its
  shape below the member folder; the `m` keeps a member apart from a year, and what is already in the
  inbox with no member folder is the first sysadmin's), so `ingest`, still the only writer of the
  catalog, records the sharer from the path. A photo arriving from a second member adds a sharer; it is not a new photo.
- **Held.** Score each new image against "a receipt", "a screenshot of a phone", "a document" with
  its SigLIP vector, plus what the catalog already knows (`is_screenshot`) and, from phase 6, the
  phone's own mark (PhotoKit flags a screenshot; the app sends it as a header and the server writes it
  to the XMP). *(review)* When: after the batch is embedded and **before the app swaps in the reloaded
  catalog** (a step of `ingest-iphone.sh` ahead of `catalog_ready`), not at `new_assets`, when there is
  no vector yet. A new photo must never be in anyone else's view before it has been scored; a photo
  with no vector is in no view at all, so a failed embed is safe. What: phone uploads from every
  member, the owner included (the Christmas receipt is his case too). Not bulk imports from the setup
  page, and not a photo another member has already shared. Over a threshold set on the demo library and checked on a sample of the real one, leaning
  towards holding, the photo is made private (`private`, `why='held'`), and the journal gets
  `photo.held`. Thresholds and prompts go in one place, with a test on demo photos.
- **Withdraw** (any sharer, any time, from the viewer on the web and on the phone): `private` with
  `why='withdrawn'`. **Release**: now, or *until a date* ("hold until Dec 26"), released by a timer
  in the app when the date passes (a background thread started in `PhotoApp.start()`, beside the
  others), journaled.
- Every member's view, the MCP tools and the sync feeds respect `private` through `visible_for`, and
  **every pixel route checks `shared_for`** before it answers (a 404, as for a sha that does not
  exist). This is the phase where a URL someone kept must stop working.
- A member finds their own private photos with `is:private` in search; a hold is also an item in their
  inbox (phase 4), one per batch.

### Phase 6: the phone gate (iPhone app)

*Built 2026-11-22 (branch `multiplayer`); [chapter 24](../book/24-uploading.md#the-phone-gate-looking-before-sharing)
describes it, [chapter 19](../book/19-iphone-app.md#starting-up) the Inbox tab, [chapter 11](../book/11-server.md#private-photos)
and appendix B the tombstones. The choices:*

- *Server: `GET` and `POST /api/tombstones` in private.py (no new module; one `tombstones.changed` entry a call).
  A refused upload is answered **200** with `X-Upload-Refused: opted_out | purged`, not a 4xx: the system uploader
  marks a 4xx job failed and the extension retries a failed job (the bytes would cross again), and the build on
  the owner's phone counts a 200 as "already there" and anything else as a failure it shows. With
  `X-Content-SHA256` the answer comes before the body (the connection then closes; URLSession on the Simulator
  read that answer correctly); without, the bytes are hashed as they arrive and the staging file removed. A purge
  refuses uploads too now, not only ingest. A catalog that cannot be read while checking is a 500, never a yes.
  `X-Screenshot` becomes the sidecar keyword `screenshot`; `private.judge` reads it (`phone_screenshot`).*
- *The phone: the rules and the state are `Shared/PhoneGate.swift`, plain functions over one JSON file in the
  app group under a file lock (the app and the extension are two processes), unit-tested on the Mac by
  `ios/Tests/run.sh` (swiftc; no Xcode test target). iOS lets only the extension make upload jobs, so **applying a
  batch sends the approved photos at once with the in-app uploader** (the app is open: the member just tapped);
  what it could not send stays approved and the extension makes jobs for it on its next run. Until the app has
  read the policy, the extension makes no job (candidates only): an owner's phone after this build is installed
  sends its new photos when the app is next opened, not before. A server with no `/api/me` (404) is
  `everything`.*
- *Review flagged: PhotoKit's screenshot and screen-recording subtypes, then Vision's fast text recognition on a
  900 px copy (six lines or 12% of the picture). Found while trying it: a review-flagged member's first batch asks
  about everything, so nothing is shared before the line about withdrawing but not erasing has been shown.*
- *The in-app uploader hashes every file it sends and keeps back, with no request, a hash in the member's
  tombstones (read on every refresh) or their unsent opt-outs; the server's refusal by header is for a list
  that is stale. A refused photo is kept back on that phone from then on (the identifier is learnt from the hash).*
- *Joining: the extension starts at its own first run, after joining; everything older is offered in the Inbox
  as "Since you joined" and then a year at a time (Share… makes a batch, Skip hides the chunk). "Upload recent
  photos" is hidden for a gated member (it would go round the review). No ranges ("skip 2014–2016" is three skips).*
- *Withdraw from the phone: the viewer's details have the web's row (Shared by you / Withdraw from the family,
  Private to you / Release) and the caption a "Private to you" mark, from `/why`'s `private`. No release on a day.*
- *A debug build launched with `-demoMember <name>` sets the `demo_member` cookie (for that launch) on every
  request and in the web views; only a `--demo` server reads it.*
- *The Simulator-only project now has the Inbox and the in-app uploader (only the Uploads screen and the
  extension stay out).*
- *Not built, or not tried: the extension recording candidates, PhotoKit's screenshot mark and a real library
  (the Simulator cannot run the extension, and marks no imported PNG a screenshot); the system uploader meeting a
  refusal; the Hidden album was tried once in the Simulator (a photo hidden in Photos was not offered or sent);
  CI does not run the Swift tests. An opted-out photo still shows in the Library as "not yet on your server"
  (LocalPhotos knows nothing of the gate). A mini item opens its page in the web view, whose own tab bar shows
  under the app's.*

- The app reads `/api/me`: its member and upload policy.
- **Everything** (the owner): as today.
- **Review** and **review flagged**: the upload extension no longer creates jobs for new photos. It
  records them as candidates. The app shows a batch as an item in its inbox (phase 4), made **on the
  phone**: the mini does not know the photos exist until they are shared. Reviewing works like a change
  set's review: everything is shared unless marked; marks are opt-outs; applying creates the upload
  jobs. *Review flagged* shows only what PhotoKit or Apple's Vision framework thinks may be private
  (screenshots, documents, text) and shares the rest.
- Never upload what is in the Photos app's Hidden album (PhotoKit leaves hidden assets out of fetches
  unless asked; keep it that way and test it).
- **Opt-outs are tombstones.** The phone hashes each resource (SHA-256, as the store does) when it is
  opted out, keeps the hash in the app's own storage, and sends the hashes (only the hashes) to
  `POST /api/tombstones`. A new phone reads its member's tombstones from `GET /api/tombstones` before its
  first upload. An opt-out can be undone only by the member who made it, on their phone (the
  tombstone's `lifted_at`).
- *(review)* **How a tombstone is enforced.** On the phone that made it, by never making the upload job:
  that phone knows the asset. On a replaced phone the asset's identifier is new, and only the hash
  matches. The in-app uploader has the file, so it sends the existing `X-Content-SHA256` and the server
  refuses a tombstoned hash before reading the body. The system uploader's jobs cannot: the extension
  never reads the file. For those the server, which already hashes the bytes as they arrive, drops a
  tombstoned upload from its staging file: nothing is written to the inbox, the store, the catalog or
  a backup. So after a phone is replaced, an opted-out photo's bytes can cross the tailnet once and be
  thrown away. The owner accepted that (open question 5).
- Photos in the Hidden album: today the extension drops them from its pending list for good, because
  `fetchAssets(withLocalIdentifiers:)` leaves them out. Keep that; a photo unhidden later is shared by
  hand.
- **Joining** starts from the day the member joined. Older photos are offered in coarse chunks ("share
  2019", "skip 2014–2016"), through the existing "Upload recent" path.
- The first share shows the line about withdrawing but not erasing (Append-only, above).
- Build and install with `ios/deploy.sh`; test on the iOS 27 simulator first.

### Phase 7: the estate's agent as a member, listeners, questions and stories

*Built 2026-11-22, the server half (branch `multiplayer`); [chapter 11](../book/11-server.md#the-estates-agent-listeners-questions-and-stories)
describes it, [chapter 26](../book/26-albums-and-agents.md#questions-and-stories) what the pages will use, appendix A
`transcribe.py`, appendix B the endpoints and `ask_member`, MAC-MINI.md the by-hand steps. Three new modules
(`listeners.py`, `stories.py`, `transcribe.py`) and one schema area (`stories`). The choices:*

- *The agent: `members.agent`, made at the app's start (before the looks are read). Its token is
  `secrets.token_urlsafe(32)` in `Tasks.runs`, in memory only, minted in `run()` and forgotten in its `finally`; it is
  in the agent's argv (the `--mcp-config` JSON), which any user of the mini can read with `ps`, but this computer is
  the sysadmin anyway. `/mcp` refuses (403) a token from elsewhere, an unknown one, and `X-Estate-Task`, so a run
  in flight at the deploy is never answered as the sysadmin (the restart marks it failed, as before). Acting for no
  one the agent reads as itself, and `private.Private.agents` makes an agent's own view NOBODY's; its albums are
  published at once as series it maintains. `agent_tasks.started_by` (`member:<id>`, `journal:<entry>/album:<id>`,
  `journal:<entry>/story:<id>`) records who or what started a run, and is how a listener knows it has. A person's
  hiding request keeps `BRIEF`; a family request (`POST /api/tasks {family: true}`) or a listener's run gets
  `WORK_BRIEF`. Who reads a task acting for no one: whoever started it and the sysadmins.*
- *A series, in the data: a list album with `change_sets.grows_from` (a search), added to and never recomputed. A
  live album is not one (it is its search already). Family series: its series lens is `maintained_by` the agent. The
  series listener acts on `ingest.finished`, which now carries `assets: [after, last]` (rowids); it waits until the
  served catalog was read after the entry (`PhotoApp.loaded_through`), then runs each series' search as its owner
  over the whole library and keeps the new photos (one search per series per ingest; restricting the mask to the
  new photos would move the search's relative floor). A member's series: `series.update_requested` and a
  `series_update` item, decided with `GET`/`POST /api/albums/<id>/candidates`.*
- *Questions: `questions` (plus `task_id`, `follow_up`), and `asked` (question, member, inbox item, answered or
  dismissed): the waiting is that row. The inbox deals on read (`GET /api/inbox`, `GET /api/questions`), with the
  app's clock: `ALLOWANCE = 3` items in any 7 days and never 3 open, or `members.questions_per_week` (one column);
  ranking undescribed photos first, then series photos, then priority, then oldest; a `group_key` is one item, and
  joins an open one for nothing. "I don't know" is for good, and counts against the week it was dealt only.*
- *Stories: `stories` and `story_photos` (a row per photo, so the purge's generic delete takes a purged photo out of
  them; `story_photos_no_delete` is in its list). The one permitted update is the transcript, once. Read by the
  teller and by whoever may be shown **every** photo of it (stricter than "one of": a story naming a photo private
  to its teller would otherwise reach the family through its other photos); the recording by the same rule. A
  recording is the request's body (audio types, 8 MiB), probed with PyAV, stored with `estate.store_blob` (no
  assets row; immutable in a marked store; the demo's beside its scratch catalog).*
- *Parakeet: parakeet-mlx 0.7's loader shells out to `ffmpeg`; transcribe.py decodes with PyAV (16 kHz mono) and
  puts it in place of that loader, so the mini needs no ffmpeg. Tried for real on the laptop (no ffmpeg on PATH):
  a `say` AAC `.m4a` and a 16 kHz WAV came back word for word, and through the running demo app end to end
  (upload, the job, `uv run transcribe.py run`, the transcript in `GET /api/photos/<sha>/stories`). The model
  (2.3 GB) is fetched by hand (`transcribe.py fetch`); without it each recording says so and is tried again.*
- *Not built: a member's own agent picking up `series.update_requested` (phase 8);
  handing an existing series to the archivist (nothing sets `maintained_by` but the archivist's own albums); "a
  one-shot task whose person is not there"; "people who know most" in the ranking; series' other two meetings with
  stories (that is the agent's work with `ask_member` and `create_album`). Known: a family question naming a photo
  that some sharer holds private is refused with a message that says it is not one the whole family can see, which
  tells the asker (who sees it) that someone holds it private.*
- *The pages and the phone, built 2026-11-23 ([chapter 26](../book/26-albums-and-agents.md#questions-and-stories),
  [chapter 19](../book/19-iphone-app.md#starting-up)). No endpoint was added or changed. One component tells a story,
  typed or spoken, on both pages: the teller in `app/story-flow.js` (a strict table: idle, asking, refused, recording,
  recorded, sending, sent, failed), mounted by inbox.html for each question (`/inbox?ask=<group>`, `app/ask-flow.js`)
  and by the viewer's details ("Tell a story about this photo", the one quiet row a photo with no story shows). The
  browser's MediaRecorder is asked for AAC in MP4 first: Safari and Chrome both make it, so every story plays on the
  iPhone; `api.js` now posts a Blob as the whole body. Transcripts are asked for again (`follow`: 3 s growing to 20 s,
  ten minutes at most), never waited on. A search's album menu makes a series (`query.growsFrom`); a series shows its
  stories, and its owner its waiting photos (`candidatesFlow`), added or none. The Ask box on `/changes` chooses for
  me or for the family; `/members` shows the agent as itself. The phone answers a question natively
  (`QuestionsView`, `Teller.swift`: AVAudioRecorder to AAC in an .m4a, `audio/mp4`, started off the main thread),
  shows a photo's stories in the viewer and tells one; its plain logic is `StoryModel.swift`, checked by
  `ios/Tests/run.sh`. The app's web views hide the page's own tab bar (a script the web view adds). Found doing it:
  the Simulator-only project dropped `DEBUG` (its target set `SWIFT_ACTIVE_COMPILATION_CONDITIONS` without
  `$(inherited)`), so `-demoMember` did nothing there. Not done: a spoken answer recorded on the Simulator (its
  input never started on this Mac: the Mac's microphone permission for Simulator, not given); a native series or
  candidates screen on the phone (those items open the web page); an answered question kept on screen after the
  phone's question screen is opened again (the server no longer lists it).*

- **The estate's agent is a member** (`kind='agent'`). Each run gets a capability, not a task number:
  `tasks.py` mints a random token for *this agent, acting for this member, for this task*, puts it in
  the MCP config in place of `X-Estate-Task`, and forgets it when the run ends. `/mcp` honours it from
  the mini only. Its permissions are the agent's role, limited to what the member it acts for can see
  (`visible_for(acting_for)`). It can propose change sets and write to the albums and lenses of the
  member it acts for; it can never apply anything to the estate.
- **The family archivist** is the estate's agent acting for no one: it sees only shared photos (no
  member's private ones) with the estate defaults applied, and publishes lenses that members may
  subscribe to.
- **Listeners.** `listeners.py`: each listener has a cursor in `listener_cursors`, reads the journal from
  it, and acts; at-least-once, so each handler is idempotent. A rule decides first, cheaply, and wakes the
  model only when the rule says yes. The first listener keeps series up to date. When photos are
  ingested, it runs each live series's search over the new photos only. If any match:
  - for a **family series** (one the archivist keeps, or one made by a one-shot task whose person is not
    there to keep it going), a run of the estate's agent decides which photos belong and adds them to
    the album directly (no proposal: a sensible start);
  - for a **member's own series**, it asks *that member's agent* to do it: a `series.update_requested`
    journal entry naming the series and the candidate photos, and a `series_update` inbox item. Their agent
    picks it up with `journal_since` (phase 8) and adds what belongs. Their agent may know things the
    estate's agent does not, which is why it is asked rather than replaced. Until it runs, the
    candidates wait, and the member can add them by hand from the inbox.
- **Every run of the estate's agent is recorded,** the implicit ones a listener starts as well as the
  ones a person submits: each is an `agent_tasks` row (its tool calls in `agent_events`, its turns and
  cost already in the row), so spending can be read off later. No cost reporting now.
- **Asking.** A new MCP tool `ask_member(shas, text, why, to?, group_key?)` files a question; the agent
  does not wait. A question may name only photos its recipient can see, or it would show a private
  photo in the question itself. The inbox, not the agent, decides how many questions a member sees: a
  few a week, ranked (photos nobody has described, people who know most, gaps in a series),
  grouped by event ("three questions about Thanksgiving 1998").
- **Answering.** In the inbox, typed or **spoken**: the phone records, uploads the audio to the store
  (content-addressed, append-only, backed up like any original), and the mini transcribes it locally
  with **NVIDIA Parakeet TDT 0.6B v2**, the open-weights model FluidVoice uses on the owner's laptop
  (*(review)* settled: the MLX port, `parakeet-mlx`, as a uv script with inline dependencies run as a
  queued job like the other model scripts; FluidAudio's Core ML build is Swift and would need a helper
  binary. Check when building that it reads the phone's recording without a system `ffmpeg`, which the
  mini does not have on the app's PATH; if it cannot, record a format it reads or install ffmpeg).
  The recording is a file in the store with no `assets` row, so it never turns up as a photo. It need not be real time: a queued job after the upload is fine. The answer is a `story`,
  attributed and dated; a second member's answer is a second story. `story.told` in the journal wakes a
  fresh run of the agent with the question, the answer and the context. No session waits for the
  answer; the waiting is the question's row.
- The viewer shows a photo's stories, with who told each and when, and plays a recording.

### Phase 8: personal agents (bring your own)

*Built 2026-11-23 (branch `multiplayer`); [chapter 11](../book/11-server.md#a-members-own-agent) describes it,
[chapter 26](../book/26-albums-and-agents.md#your-own-agent) the page, appendix B the tool, the endpoints and the page,
MAC-MINI.md the by-hand steps. No new module: the bindings are members.py's, the filter journal.py's. The choices:*

- *A device is the tailnet node. Tailscale's serve documentation (kb/1312/serve) lists the identity headers it adds,
  `Tailscale-User-Login`, `Tailscale-User-Name` and `Tailscale-User-Profile-Pic`, user fields only, none for tagged
  nodes; its source (ipn/ipnlocal/serve.go) also sets `X-Forwarded-For` to the source address, replacing the client's,
  and `Tailscale-Headers-Info`. So the app asks `tailscale whois --json` for that address's `Node.StableID` and name
  (cached ten minutes); without whois, the address is the device. With `--demo`, `X-Demo-Device`, else "this
  computer". Not tried on the mini: whois from the app's user, and what serve forwards for a second user's node.*
- *One `agent_bindings` row per member and node (columns beyond the plan's: `node`, the key, and `seen_at`). States:
  pending (looks only; every writing tool says to allow it, where), approved (writes as the member), revoked (every
  call refused, 403: turned off means it no longer acts as them at all). Only the member decides, on the web
  (`POST /api/agents/<id>`), and **allowing must come from another device than the agent's own**: an agent with a
  shell holds the member's login on its device and could post anything a browser there could. The owner approves
  his laptop's agent from his phone. Turning off works from anywhere.*
- *Journaled as the member (actor), with `{"binding": <id>}` in each entry's data, set around a tool call
  (`journal.by_agent`): no member row per personal agent, no `acting_for` (that means the estate's agent).*
- *Tools: the estate's agent's for its member, less the three task tools (refused in words if called) and
  `ask_member`'s `follow_up`; plus `journal_since(cursor, limit)`, offered to own agents only, and `amend_album`'s
  `request_done`, which is the existing candidates write with nothing more to add. Own agents get their own
  instructions text (no "Handing it over").*
- *`journal_since`: a kind not listed is never returned; journal.py's docstring has the table, and the test appends
  every listed kind as Ada and as Ben and checks each against it. Included with a reason: the member's own doings
  (their privacy is theirs), published series (counts dropped: they could include the owner's private photos) and
  lenses, an ingest's photos the member may be shown (not before the served catalog has them), stories they may
  read, their series' requests (with `waiting`). Left out: members, faces and people, tasks, migrations, purges,
  opt-outs, and anything about another member's private photos, albums, hides, favourites, questions or runs.*
- *Connect your agent (`/agents`, `agents.html`, `app/agents-flow.js`): the command from the request's own address
  (`X-Forwarded-Host`), with the demo's two headers on the demo; a configuration for other harnesses; where it can
  run (Anthropic's help page says custom connectors in claude.ai, Desktop and the phone app connect "from Anthropic's
  cloud infrastructure", so not over the tailnet: from the documentation, not tried); the member's agents, and a
  refresh every 4 s (20 s after two minutes) while visible. The inbox's `binding` item opens it.*
- *With it, two fixes from review: the viewer's "Shared by you" card puts "Withdraw from the family" under its words;
  a story's recording keeps the length the teller's page measured (`stories.seconds`, `?seconds=` on the upload),
  shown beside its player, since a browser's fragmented MP4 says nothing of its length until played (the route
  already answered `Content-Length` and `Range`; the player still says 0:00 until played; the phone sends none).*
- *Not built: a list of an agent's writes on the page; a sysadmin's view of everyone's bindings; push to the agent
  (it polls `journal_since`); `stories.seconds` is not under the stories trigger (it is set once, at insert).*

- A member's own agent, in their own harness, calls `https://<mini>.ts.net:8766/mcp` over the tailnet:
  `tailscale serve` adds the member's login, so the estate knows who it is with no key to copy. A call
  on `/mcp` with a login is that member's agent.
- **Binding.** The first call from a device makes an `agent_bindings` row and a `binding` inbox item:
  "An agent connected as you from *alice-macbook*. Allow it?" Until approved it can only read. Once
  approved it writes, in the journal, as that member's agent; the member can revoke it.
- A personal agent gets the same tools as the estate's agent acting for its member, except
  `submit_task`: personal work runs in the member's own harness, on their own account, never on the
  owner's subscription. It also gets `journal_since(cursor)`: what changed in what its member can see,
  including the `series.update_requested` entries for its member's series (phase 7), so when it
  connects it catches up and updates those series. Marking the request done closes the inbox item.
- **"Connect your agent"**, a page that shows the exact command with the mini's address filled in
  (`claude mcp add --transport http photo-estate https://<mini>.ts.net:8766/mcp`), a configuration file
  for other harnesses, what phase 0 found about where it can run, and the connection as it happens
  ("connected as Alice at 3:42 PM: approve it in your inbox").

## Testing

- Python: a unit test for each new module (`members`, `journal`, `lenses`, `member_inbox`, `listeners`),
  `tests/test_multiplayer.py` for the rules of what a member sees across members, lenses, sharers and
  private photos (the riskiest code: test every row of the rules, that every route uses `visible_for`,
  and that someone else's series hides what my filters hide and what is private; that every pixel
  route refuses a photo private to someone else; that a stack whose best shot I have hidden still
  shows its next best), the protected-table
  triggers, the purge tool on a scratch catalog, and the sources
  only adding.
- The MCP tools for each kind of caller: the estate's agent acting for a member, acting for no one, a
  personal agent bound and not yet bound, and that nothing private leaks through a question.
- The pages: Playwright on the demo library as each demo member (`X-Demo-Member`), light and dark
  screenshots of `/members`, `/inbox`, the lens views and the "why" line.
- The iPhone app: the simulator (iOS 27) for the phone gate, opt-outs, the Hidden album and a new phone
  reading its tombstones.
- `uv run pytest`, `uv run ruff check .`, and in `app/` `npm run typecheck` and `npm test`, all green
  in every phase. The checked-in demo library (`demo/`) does not change; demo members are made at start.

## Not in this plan

- Access control beyond *private to the uploader* (who may see what).
- Push notifications; the inbox in the app is enough for now.
- Devices as members (a photo frame showing a lens): the `viewer` role is there so it can come later.
- Succession of the sysadmin role.
- The link to the knowledge estate (the journal and the stories are what it will read).
- Backup, the NAS and the rotating drives: a separate plan, this week's work, and a prerequisite in
  spirit. Append-only does not help if the one disk the store is on fails.
- A public `/mcp` or any way in that is not the tailnet.

## Open questions

1. **The categories held at ingestion** beyond receipts, screenshots and documents (gifts and medical
   photos are harder to tell).
2. **How many questions a week**, and whether an elder gets more.

Those two can wait for their phases (5 and 7). The review of 2026-11-22 left four more; the owner
answered three the same day:

3. **What the family sees of the library as it is today.** *Decided:* nobody but the owner is approved
   until the hold scoring has run once over the whole library, as the owner's, and he has looked over
   what it held. What he has hidden stays an estate default members may opt out of: hidden has meant
   "out of my way", not "private". So phase 5 lands before anyone else is let in, and phases 1 to 4
   are used by the owner and the demo members only.
4. **Who may start a run of the estate's agent**, which spends the owner's subscription. *Decided:* any
   member, audited: every run is an `agent_tasks` row with who asked, and a journal entry.
5. **A tombstone after a phone is replaced** (phase 6). *Decided:* the server discards. An opted-out
   photo's bytes may reach the mini once from a replaced phone and are dropped from the staging file,
   unread and unkept.
6. **Who joins first, and how.** *Decided:* an agent, before any person. The owner invites a second
   account of his own (another sign-in provider) into the tailnet as an ordinary user; that account
   is also what phase 0 needs. The **test member** is a member of `kind='person'`: it asks for access,
   is approved, and has a role and an inbox like anyone, so it walks the path a person will. It runs
   on the laptop, through a second `tailscaled` in userspace mode signed in as that account (to be
   proved in phase 0), and is driven with Playwright and `curl`.
   - It joins after phase 5. Its first job is to try to see what it should not: kept photo URLs, the
     sync feed, the MCP tools, the `why` line, someone else's series, with the owner holding private
     photos. Then the whole join flow, and in phase 8 connecting as a personal agent.
   - On the real library it works with ids and the API only, never screenshots: the estate's agent
     was built never to see pixels, and a simulated user should not be the exception. Looking at
     pages is done on the demo library, as the demo members.
   - It uploads nothing to the real estate (an upload is there for good): upload, hold and tombstone
     tests run against a scratch copy.
   - It cannot be a phone (the simulator shares the laptop's network, so it is the owner) and cannot
     say whether a person understands the wording. The first person joins once what it found is
     fixed, and the phone gate is first tried on that person's phone.
   - Before the invitation, the tailnet's policy gets one grant: `group:family` to the mini (both
     address families) on `tcp:8766`, with a test that the group reaches nothing else. The policy is
     already default-deny and grants by device (hardened 2026-10-25), so a new user reaches nothing
     until that grant names the group; each later member is one more address in the group.
