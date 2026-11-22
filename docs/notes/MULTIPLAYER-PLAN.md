# Multiplayer: members, lenses, the phone gate, the journal, the inbox and agents as players

Written 2026-11-22, from a design conversation with the owner. Nothing here is built yet. This note is
the brief for building it: what multiplayer means for Photo Estate, the decisions already made (and
why), the data model, and the work in phases, each phase a pull request that can be deployed on its own.
When a phase lands, the book describes the result and this note keeps the reasoning.

Read [CLAUDE.md](../../CLAUDE.md) first: how to run the app on the demo library, the tests, the rules for
the script modules and for the docs, and how a branch reaches the Mac mini.

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
| The **phone gate**: a member may review each batch on the iPhone before it is uploaded. An opt-out is permanent | Some family members will not want to share everything. A photo opted out on the phone never reaches the mini, so no backup, agent or person can see it |
| Opt-outs are kept as content hashes (tombstones), on the phone and mirrored to the mini | A tombstone keyed by the asset's identifier would be lost when the phone is replaced, and the next sync would upload everything that had been opted out |
| **Append-only**: hiding, withdrawing, every change to a lens or album is an entry in the journal; undoing is another entry. Only the sysadmin purges, by hand, outside the app | Avoiding accidental deletion matters more than almost anything else. Members are told before they share that a shared photo can be withdrawn from view but not erased |
| A **journal**: one append-only log of everything that happens, each entry with its actor | It gives listeners something to react to, explains every hidden photo ("hidden by Mom's *No screenshots* lens"), and is what the knowledge estate will read |
| An **inbox** per member: everything that needs a person's decision goes there | Every "propose" in this design ends in someone deciding. The in-app inbox is enough for now: no push notifications |
| A **sysadmin** role (the owner, for now) | Someone approves new members, decides the estate's default lenses and holds the purge. Succession is a later plan; the role is a row, not code, so it can be handed on |
| Agents: the **estate's agent** runs on the mini on the owner's subscription and does family work. A **personal agent** runs in the member's own harness, on their own account, and connects to `/mcp` over the tailnet | Clean from the beginning: the owner pays for family work; whoever wants a personal agent brings and pays for it |
| An agent acts for someone: it can see only what that member can see, and writes only to what that member owns | Otherwise an agent can be used to reach what its user cannot |
| New photos go straight into a story album, with no proposal step | A sensible start. More review can be added after some use |
| Answers to an agent's questions can be spoken; the recording is kept in the estate | For an oral history the voice is worth more than the transcript |

## Words

These are new words in the code and the book. Some old ones are taken, so mind the collisions:

- **member**: a person or an agent the estate knows (later a device, such as a photo frame). Not
  `persons`, which are the people *in* photos (faces.py). A member may be linked to a person.
- **login**: a Tailscale login (`alice@icloud.com`), mapped to one member. A member may have several.
- **role**: `sysadmin`, `member` or `viewer` (`viewer` is for devices later; nothing uses it yet).
- **sharer**: a member who uploaded a photo. A photo the whole family photographed or forwarded may have
  several. Ingestion merges copies by content hash, so "who uploaded it" is a set, not one member.
- **private**: a photo that is private to a sharer: only that sharer sees it. Reached by *held*
  (ingestion) or *withdrawn* (the sharer). Leaving it is *released* (optionally *until* a date).
- **lens**: a named view. A *filter* lens leaves photos out; a *story* lens picks and orders photos
  (an album). Live (follows a search, or is kept up to date by an agent) or a snapshot. Private or
  published. An **estate default** is a published filter lens every member is subscribed to unless they
  opt out.
- **journal**: the append-only log. Not `events`: that name is taken three times already (the catalog's
  photo clusters in places.py, the setup page's job log in intake.db, and the pipeline protocol in
  `core/events.py`).
- **inbox**: a member's list of things to decide. In code, `inbox_items`. The word "inbox" is also the
  upload folder (`--inbox`, and the sweep that ingests it: `upload.IngestJob`, started by `photo_app.py`); leave that alone and name the new code
  `member_inbox.py` and the table `inbox_items`, so the two are never confused.
- **tombstone**: the content hash of a photo a member opted out of, or one the sysadmin purged: never
  accepted again.
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
CREATE TABLE tombstones(sha256 TEXT PRIMARY KEY, member_id INT, why TEXT, at TEXT);   -- opted out | purged

-- views
CREATE TABLE lenses(
  id INTEGER PRIMARY KEY, owner INT, title TEXT,
  kind TEXT,                                             -- filter | story
  set_id INT,                                            -- change_sets.id holding its photos (and query)
  published INT DEFAULT 0, estate_default INT DEFAULT 0,
  maintained_by INT,                                     -- an agent member that keeps it up to date, or NULL
  created_at TEXT);
CREATE TABLE subscriptions(member_id INT, lens_id INT, mode TEXT, at TEXT,   -- live | snapshot | opted_out
  PRIMARY KEY(member_id, lens_id));
CREATE TABLE shown(sha256 TEXT, member_id INT, at TEXT, PRIMARY KEY(sha256, member_id));  -- "show it anyway"

-- deciding and asking
CREATE TABLE inbox_items(
  id INTEGER PRIMARY KEY, member_id INT, kind TEXT,      -- held | review | lens_proposed | lens_changed |
                                                         --   binding | question | ...
  ref TEXT, title TEXT, created_at TEXT, state TEXT, done_at TEXT);   -- open | done | dismissed
CREATE TABLE questions(
  id INTEGER PRIMARY KEY, asked_by INT, acting_for INT,
  to_member INT,                                         -- NULL: open to the family
  shas TEXT, text TEXT, why TEXT, group_key TEXT,        -- group_key: ask about one event together
  priority REAL, status TEXT, created_at TEXT);          -- queued | asked | answered | closed
CREATE TABLE stories(
  id INTEGER PRIMARY KEY, member_id INT, question_id INT, shas TEXT,
  text TEXT, recording TEXT,                             -- recording: the sha256 of the audio in the store
  told_at TEXT);
CREATE TABLE listener_cursors(name TEXT PRIMARY KEY, journal_id INT);
```

Favourites become personal through a new table, because `favorites` is keyed by the photo alone and a
primary key cannot change additively: `member_favorites(sha256, member_id, fav, set_at)`. The existing
rows are copied into it as the sysadmin's, and `favorites` is no longer written. Existing tables gain a
column: `change_sets.owner`, `agent_tasks.acting_for`. Old rows belong to the sysadmin.

### What a member sees

For member *M*, a photo is visible when all of these hold:

1. it is shared: *M* is one of its sharers, or some sharer has it and has not made it private
   (a photo shared by anyone is shared: my opt-out decides my copy, not yours);
2. no filter lens *M* is subscribed to (live, or the snapshot taken) hides it, unless *M* has said
   "show it anyway" (`shown`);
3. it is not a paired Live Photo clip, as today.

`Catalog` keeps the photo arrays it has today. What changes is that `visible` becomes a mask per member,
worked out once and cached until a journal entry that touches visibility arrives. A mask is one bit per
photo, so 100k photos for a dozen members is about 150 KB. Every route that reads `visible` (search,
facets, Years and Months, trips, People, `/api/info`, the sync feed, the MCP tools) reads the mask of
the member the request is for. Getting this right everywhere is the riskiest part of the work, so phase
3 starts by sending every reader of `visible` through one function and testing that function.

The sync feed becomes one feed per member (`sync-feed-<member id>.db` beside the catalog), so a photo
that one member's lens hides leaves only that member's phones.

### What is shared, and what is personal

| Shared (facts of the estate, every change journaled with its actor) | Personal (the member's own) |
|---|---|
| The photos and their metadata, places, events, trips | Filter lenses and what they hide; "show it anyway" |
| People: naming faces, merging persons | Favourites |
| Stories, recordings, answers | Albums and story lenses (until published) |
| Published lenses and estate defaults | Subscriptions |
| | Private photos, their holds and releases |

## Append-only

- **Journal first.** Every write in this plan appends its journal entry in the same transaction as the
  state it changes. State tables (`hidden`, `subscriptions`, `shown`, `private`...) may still be updated
  and have rows deleted: they are the *current* state, and the journal says how it got there. Undo is a
  new entry that reverses the state, never a deleted entry.
- **Protected tables.** SQLite triggers make `DELETE`, and `UPDATE` of anything but the stated columns,
  raise on: `journal`, `assets`, `occurrences`, `sharers`, `tombstones`, `stories`, `members` and
  `member_logins` (a member is revoked, never deleted). The purge tool (below) is the one thing that
  drops a trigger, inside its own transaction.
- **Albums are not deleted.** `changes.py`'s discard marks a set `discarded` and journals it rather than
  deleting its rows. The face labels' undo in `faces.py` stays as it is (it is shared state), and is
  journaled.
- **Immutable originals.** Ingestion sets the user-immutable flag (`chflags uchg`) on each file it writes
  to the store, so even `rm -rf` in the wrong folder fails. A tool for setting it on the files already
  in the store runs once on the mini, after a backup.
- **Deletions upstream never propagate.** A photo deleted on an iPhone, in iCloud or in the OneDrive
  mirror stays in the estate. Sources only add. Write a test for each source that removes a file and
  re-ingests, and checks the catalog and the store are unchanged.
- **Purge.** `estate.py purge <sha> --reason "..."`: run by hand on the mini by the sysadmin, never from a
  page, a phone or an agent. It takes a catalog backup first (`.backup`, then `pragma quick_check` on the
  copy), appends a `photo.purged` journal entry (who, when, why: the fact survives the bytes), adds a
  tombstone so no source brings the photo back, clears the immutable flag and removes the file. It says
  that the photo remains in catalog backups until they rotate out.
- **Members are told.** The phone gate's screen says, before the first share: shared photos become part
  of the family estate; you can withdraw one from view later, but not erase it.

## Phases

Each phase is a pull request, with its tests, its docs (the book chapter it changes, appendix B for each
endpoint, chapter 2's file list, the glossary), and a deploy of its branch to the mini as CLAUDE.md
describes. Every phase that touches the catalog's schema takes the backup CLAUDE.md gives first. Phases
1 and 2 come first and in that order; 3 to 6 follow, and 7 and 8 need 6.

### Phase 0: check three things first (a day, no code)

1. **The identity header for every way in.** Tailscale offers two ways to let family reach the mini:
   inviting them as users of the owner's tailnet, or *sharing* the mini with their own tailnets. Check
   on the mini with a second account that `tailscale serve` sets `Tailscale-User-Login` for both, and
   what it sets for the owner's own tagged devices (if any). The plan assumes invitations, with a
   `group:family` in the tailnet's default-deny policy granting only the app's port, and needs the
   header to be there. Check the plan's user limit too.
2. **The estate's agent subscription.** The estate's agent runs on the owner's Claude subscription
   (`~/.config/photo-estate/agent.env`). Read Anthropic's current terms for a subscription used for
   work done for other people; if it does not fit, the estate's agent uses an API key, and `tasks.py`
   reads either.
3. **Where personal agents can run.** A personal agent reaches `/mcp` only from a device on the tailnet
   (Claude Code; a desktop app with a local MCP configuration). Connectors added in claude.ai or the
   Claude iPhone app are expected to be called from Anthropic's servers, which are not on the tailnet.
   Confirm, and write the answer into the "Connect your agent" page. The tailnet stays closed: no
   Tailscale Funnel, no public `/mcp`.

### Phase 1: members and identity

- The tables `members`, `member_logins` and `journal`, and the module `members.py` (with its unit test):
  look a login up, add a member, change a role or status. The journal table lands here so member
  changes are journaled from the first day; phase 2 journals everything else and adds the protections.
- `who(headers)` returns a member, not a string. A login with no member gets **a request-access page**:
  "Ask John to let you in", which makes a `pending` member and an inbox item for the sysadmin (the page
  can be plain until phase 6; until then the sysadmin approves on `/members`).
- Roles replace `--upload-user`. On first start with the new code, the catalog has no members: the
  logins given by `--upload-user` become active members, the first of them the sysadmin (or
  `--sysadmin <login>`, a new option). Old `created_by`/`set_by` logins become those members.
- `Auth` becomes role-based: `MEMBER` (an active member), `SYSADMIN`, and `LOCAL` (a request from the
  mini itself, kept for the CLI, the tools and the tests, and treated as the sysadmin). The default flips
  from "anyone who can reach the app may write" to "only members may do anything at all"; only the
  request-access page, the home-screen app files and `/api/info`'s version are open.
- `/mcp` no longer trusts being local (phase 7 gives the estate's agent its token; until then the
  `X-Estate-Task` header keeps working as today, so nothing breaks in between).
- `/members` (sysadmin): who is in, their logins, roles, upload policies; approve, revoke, link a member
  to a person in People.
- `GET /api/me`: the member, role, upload policy. The iPhone app reads it.
- **Demo and preview.** `--demo` makes three members (a sysadmin and two members) and accepts
  `X-Demo-Member: <name>` in place of the Tailscale header, *only* with `--demo`, so Playwright tests
  and the cloud preview can be each of them. Never honour it without `--demo`.

### Phase 2: the journal and append-only

- The `journal` table and `journal.py` (unit-tested): `append(con, actor, kind, subject, data,
  acting_for=None)` in the caller's transaction, and `since(id, kinds=None)` for listeners. A
  catalog of the kinds in one place (like `core/states.py`'s StrEnums).
- Journal every write that exists today: change sets (propose, items, decide, apply, revert, commit,
  reopen, discard), albums, favourites, People (name, merge, not, later), tasks, uploads arriving,
  ingest finishing. Each change set row's "created by" becomes the journal's actor.
- The protected-table triggers, discard-as-mark, the immutable flag at ingest and the one-off tool for
  the existing store, the purge tool, and the "upstream deletions never propagate" tests.
- `GET /api/journal?since=&kinds=` (sysadmin; members get their own entries and those about photos they
  can see).
- On the mini: the migration that gives every existing photo the sysadmin as its sharer (shared), and
  makes the existing hiding sets the first estate default (see phase 3), runs after a backup.

### Phase 3: lenses and what each member sees

- `lenses.py` (unit-tested): a lens is a row of `lenses` over a change set (`changes.py` keeps doing
  the work of holding photos, sections, reasons and queries). A filter lens's photos are hidden from
  its subscribers; a story lens is an album with an owner.
- One function, `visible_for(member)`, the only way any route asks what a member sees, built from the
  rules in "What a member sees" above, cached per member, cleared by journal entries about
  visibility. First move every reader of `Catalog.visible` onto it with the same answer as today,
  then change what it computes.
- The existing global hiding becomes the lens **Estate defaults** (owner: the sysadmin, published,
  estate default), so the family first sees the library as the owner curated it, and can opt out.
- Personal hiding: the review page applies a change set to *my lens* (the default) or, for the
  sysadmin, to an estate default.
- Subscriptions: subscribe to a published lens, live or as a snapshot (a snapshot copies its photos into
  a lens of your own); opt out of an estate default; "show it anyway" for one photo. A live lens that
  changes makes an inbox item for its subscribers in phase 6 (`lens_changed`), and the change applies
  at once.
- **Explain it**: `GET /api/photos/<sha>/why` answers why a member does not see a photo ("hidden by
  Mom's *No screenshots* lens on Nov 3"), from the journal; the viewer shows it.
- Proposing an estate default: any member can publish a filter lens and propose it; the sysadmin
  decides (an inbox item in phase 6).
- Favourites per member. Albums get an owner and `published`.
- Per-member sync feeds; the iPhone app is unchanged apart from receiving its member's feed.
- The search page's Albums tab shows lenses: mine, published by others (subscribe), estate defaults.

### Phase 4: sharers, private photos, the hold at ingestion

- **Who shared it.** Uploads land in `<inbox>/<member id>/YYYY/MM/DD/...` (the upload path keeps its
  shape below the member folder), so `ingest`, still the only writer of the catalog, records the sharer
  from the path. A photo arriving from a second member adds a sharer; it is not a new photo.
- **Held.** After a batch is ingested (the `new_assets` event of `core/events.py`), score each new image
  against "a receipt", "a screenshot of a phone", "a document" with the SigLIP model already loaded,
  plus the phone's own marks (a screenshot is flagged by PhotoKit; the iPhone app sends it in the
  XMP). Over a threshold set on the demo library and checked on a sample of the real one, leaning
  towards holding, the photo is made private (`private`, `why='held'`), and the journal gets
  `photo.held`. Thresholds and prompts go in one place, with a test on demo photos.
- **Withdraw** (any sharer, any time, from the viewer on the web and on the phone): `private` with
  `why='withdrawn'`. **Release**: now, or *until a date* ("hold until Dec 26"), released by a timer
  in the app when the date passes (a background thread started in `PhotoApp.start()`, beside the
  others), journaled.
- Every member's view, the MCP tools and the sync feeds respect `private` through `visible_for`.

### Phase 5: the phone gate (iPhone app)

- The app reads `/api/me`: its member and upload policy.
- **Everything** (the owner): as today.
- **Review** and **review flagged**: the upload extension no longer creates jobs for new photos. It
  records them as candidates. The app shows a batch as an item in its inbox (phase 6), made **on the
  phone**: the mini does not know the photos exist until they are shared. Reviewing works like a change
  set's review: everything is shared unless marked; marks are opt-outs; applying creates the upload
  jobs. *Review flagged* shows only what PhotoKit or Apple's Vision framework thinks may be private
  (screenshots, documents, text) and shares the rest.
- Never upload what is in the Photos app's Hidden album (PhotoKit leaves hidden assets out of fetches
  unless asked; keep it that way and test it).
- **Opt-outs are tombstones.** The phone hashes each resource (SHA-256, as the store does) when it is
  opted out, keeps the hash in the app's own storage, and sends the hashes (only the hashes) to
  `POST /api/tombstones`. A new phone reads its member's tombstones from `GET /api/tombstones` before its
  first upload. Every upload carries `X-Estate-SHA256`; the server refuses a tombstoned hash before
  reading the body. An opt-out can be undone only by the member who made it, on their phone.
- **Joining** starts from the day the member joined. Older photos are offered in coarse chunks ("share
  2019", "skip 2014–2016"), through the existing "Upload recent" path.
- The first share shows the line about withdrawing but not erasing (Append-only, above).
- Build and install with `ios/deploy.sh`; test on the iOS 27 simulator first.

### Phase 6: the inbox

- `member_inbox.py` (unit-tested) and `inbox_items`. Items are made by the code that needs the
  decision: `held` (photos held at ingestion, grouped per batch), `review` (a change set to look over),
  `lens_proposed` (for the sysadmin), `lens_changed` (for subscribers), `binding` (phase 8),
  `question` (phase 7), `member_request` (for the sysadmin). Each kind opens the review screen it
  already has, or a new one, as an app of its own (the change set review page is the first).
- `/inbox` on the web, with its state machine in `app/inbox-flow.js` (strict `flow()`, unit test in
  `app/tests/`), and an Inbox tab in the iPhone app with a badge. The phone's own upload batches show in
  the same list, beside the mini's items.
- No push notifications for now.

### Phase 7: the estate's agent as a member, listeners, questions and stories

- **The estate's agent is a member** (`kind='agent'`). Each run gets a capability, not a task number:
  `tasks.py` mints a random token for *this agent, acting for this member, for this task*, puts it in
  the MCP config in place of `X-Estate-Task`, and forgets it when the run ends. `/mcp` honours it from
  the mini only. Its permissions are the agent's role, limited to what the member it acts for can see
  (`visible_for(acting_for)`). It can propose change sets and write to the albums and lenses of the
  member it acts for; it can never apply anything to the estate.
- **The family archivist** is the estate's agent acting for no one: it sees only shared photos (no
  member's private ones), and publishes lenses that members may subscribe to.
- **Listeners.** `listeners.py`: each listener has a cursor in `listener_cursors`, reads the journal from
  it, and acts; at-least-once, so each handler is idempotent. A rule decides first, cheaply, and wakes the
  model only when the rule says yes. The first listener keeps story lenses up to date: when photos are
  ingested, it runs each live story lens's search over the new photos only; if any match, a run of the
  estate's agent decides which belong and adds them to the album directly (no proposal: a sensible
  start). That run acts for the lens's owner. (This spends the owner's subscription on personal lenses:
  see Open questions.)
- **Asking.** A new MCP tool `ask_member(shas, text, why, to?, group_key?)` files a question; the agent
  does not wait. A question may name only photos its recipient can see, or it would show a private
  photo in the question itself. The inbox, not the agent, decides how many questions a member sees: a
  few a week, ranked (photos nobody has described, people who know most, gaps in a story lens),
  grouped by event ("three questions about Thanksgiving 1998").
- **Answering.** In the inbox, typed or **spoken**: the phone records, uploads the audio to the store
  (content-addressed, append-only, backed up like any original), and the mini transcribes it locally
  (the model is the phase's choice: it must run on the mini, offline). The answer is a `story`,
  attributed and dated; a second member's answer is a second story. `story.told` in the journal wakes a
  fresh run of the agent with the question, the answer and the context. No session waits for the
  answer; the waiting is the question's row.
- The viewer shows a photo's stories, with who told each and when, and plays a recording.

### Phase 8: personal agents (bring your own)

- A member's own agent, in their own harness, calls `https://<mini>.ts.net:8766/mcp` over the tailnet:
  `tailscale serve` adds the member's login, so the estate knows who it is with no key to copy. A call
  on `/mcp` with a login is that member's agent.
- **Binding.** The first call from a device makes an `agent_bindings` row and a `binding` inbox item:
  "An agent connected as you from *alice-macbook*. Allow it?" Until approved it can only read. Once
  approved it writes, in the journal, as that member's agent; the member can revoke it.
- A personal agent gets the same tools as the estate's agent acting for its member, except
  `submit_task`: personal work runs in the member's own harness, on their own account, never on the
  owner's subscription. It also gets `journal_since(cursor)` (what changed in what its member can see),
  so it can catch up when it connects.
- **"Connect your agent"**, a page that shows the exact command with the mini's address filled in
  (`claude mcp add --transport http photo-estate https://<mini>.ts.net:8766/mcp`), a configuration file
  for other harnesses, what phase 0 found about where it can run, and the connection as it happens
  ("connected as Alice at 3:42 PM: approve it in your inbox").

## Testing

- Python: a unit test for each new module (`members`, `journal`, `lenses`, `member_inbox`, `listeners`),
  `tests/test_multiplayer.py` for the rules of what a member sees across members, lenses, sharers and
  private photos (the riskiest code: test every row of the rules, and that every route uses
  `visible_for`), the protected-table triggers, the purge tool on a scratch catalog, and the sources
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

1. **Who pays to keep a personal story lens up to date?** The decision is that the owner's subscription
   pays for family work and members bring their own agent for personal work, but a personal agent in
   someone's harness on their laptop cannot be woken by the mini when new photos arrive. Choices: (a) the
   estate's agent keeps every story lens up to date (cheap with the rule first, but it is personal work
   on the owner's subscription); (b) the mini's rule adds candidates to the member's inbox and their own
   agent decides when it next connects (`journal_since`); (c) story lenses the family shares are family
   work, private ones wait for (b). Phase 7 builds (a) behind a switch; decide before phase 8.
2. **The categories held at ingestion** beyond receipts, screenshots and documents (gifts and medical
   photos are harder to tell).
3. **How many questions a week**, and whether an elder gets more.
4. **The transcription model** for spoken answers.
