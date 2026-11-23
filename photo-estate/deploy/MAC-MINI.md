# Running the photo app as an always-on service

The photo app (`embed.py serve`) is an ordinary local web server on `127.0.0.1:8766`. To use it
from your phone and other computers, run it on a machine that stays on, start it at boot, and put
something in front of it that your devices can reach. This page walks through my setup, a Mac mini
with a USB disk and Tailscale. The same pieces exist everywhere:

| | My setup (macOS) | On Linux |
|---|---|---|
| Start at boot, restart on crash | launchd agent ([plist](com.jflam.photo-search.plist)) | a systemd user service (`loginctl enable-linger` to run without a login) |
| Reach it from other devices | `tailscale serve` (HTTPS, tailnet only) | the same, or Caddy/nginx on your LAN |
| Disk permissions | a one-time macOS dialog (below) | none |

Change the label, user and paths in the plist to yours.

My estate moved from a Linux laptop to the always-on Mac mini (`johns-mac-mini`, M1, 16 GB) on
2026-11-11. The photo app runs there as a launchd agent and is published to my tailnet only.

**Open it:** `https://johns-mac-mini.<tailnet>.ts.net:8766`

## The disk

The 8 TB Seagate One Touch is **APFS**, volume name `estate`, mounted at `/Volumes/estate`. It
was btrfs (see [DISK-SETUP.md](../../docs/guides/DISK-SETUP.md)), which macOS can't read. It was reformatted after copying
everything off and back, verified by file count, byte total and the `estate.db` SHA-256.

Spotlight is turned off for the volume (`sudo mdutil -i off /Volumes/estate`). The app has its own
search, and indexing 120k photos slows every big copy.

## The catalog is on the internal SSD

Since 2026-11-20 the catalog is not on that disk. The photos are big and read front to back, which a
hard disk does well; the catalog is read in small pieces from all over the file, which it does badly.

| On the SSD, in `~/estate/` | On the USB disk, in `/Volumes/estate/` |
|---|---|
| `estate.db` (1.5 GB), `sync-feed.db`, `intake.db` and `intake/` | `store/`: the photos |
| `face-labels.jsonl`, `query-log.jsonl`, `stats-cache.json` | `iphone/` and `iphone.partial/`: uploads from the phone (`--inbox`) |
| | `review/`: thumbnails and face crops (`--cache`) |
| | `catalog-backups/` (`--backup-dir`): now on a different disk from what it backs up |

The app finds the small files beside `--db`, so they moved with it; `--inbox` and `--cache` keep the
big folders where they were. Measured on the real library (113,634 photos, 110,020 faces) at a restart:

| | Catalog on the USB disk | On the SSD |
|---|---|---|
| The page answers | 22 s | 7 s |
| Reading the catalog | 18.9 s | 4.4 s |
| Reading the faces (People) | 50-68 s | 4.1 s, and under 2 s with the join taken out of `faces.py` |

How it was moved (the app stopped throughout): `PRAGMA wal_checkpoint(TRUNCATE)`, then `sqlite3
old ".backup new"` for each of the three databases, `PRAGMA integrity_check` and a row count of every
table compared with the original; the three plain files copied and compared; then the originals renamed
`*.moved-20261120` on the USB disk (delete them once you are sure), and the plist changed. To go back:
stop the app, rename them back, restore the plist (`~/photo-estate-lab/com.jflam.photo-search.plist.before-ssd-move`).

Scripts run by hand need to be told, because their default is everything under one folder:

```bash
DB=~/estate/estate.db STORE=/Volumes/estate/store INBOX=/Volumes/estate/iphone CACHE=/Volumes/estate/review \
  ./photo-estate/ingest-iphone.sh ~/estate
uv run photo-estate/faces.py --db ~/estate/estate.db run --store /Volumes/estate/store --cache /Volumes/estate/review
```

## The service

| | |
|---|---|
| Code | `~/src/photo-estate` (a clone of this repo) |
| launchd agent | `~/Library/LaunchAgents/com.jflam.photo-search.plist` (copy in this folder) |
| Log | `~/Library/Logs/photo-search.log` |
| Catalog | `~/estate/estate.db`, on the internal SSD (see above) |
| Query log | `~/estate/query-log.jsonl` (next to the catalog) |
| Catalog backups | `/Volumes/estate/catalog-backups/` (`--backup-dir`): the app copies `estate.db` and `face-labels.jsonl` there daily and keeps 7 |
| Local port | `127.0.0.1:8766` |
| Tailnet | `tailscale serve --bg --https=8766 http://127.0.0.1:8766` |

It starts at login and restarts if it exits. The page answers about 7 seconds after a restart
(about 20 when the catalog was on the USB disk); typed searches wait about 15 seconds more for the
text encoder, which loads in the background. It's a per-user agent, so the Mac must be logged in after a reboot (automatic login in
Users & Groups covers this).

Install or reinstall:

```bash
cp photo-estate/deploy/com.jflam.photo-search.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jflam.photo-search.plist
```

Restart after pulling new code:

```bash
launchctl kickstart -k gui/$(id -u)/com.jflam.photo-search
```

Stop it while running a long ingest or embedding job:

```bash
launchctl bootout gui/$(id -u)/com.jflam.photo-search
```

## Thumbnails on the internal SSD

The estate disk is a USB *hard* disk. A thumbnail that isn't in memory costs a seek of about 9 ms,
and seeks don't overlap, so the first screenful of 150 took about a second however many requests
ran at once. `--fast-cache ~/Library/Caches/photo-estate` keeps thumbnails, previews and large
views on the internal SSD. Anything missing there is copied from
`/Volumes/estate/review` on first use, or decoded from the original if neither has it. At startup
the server copies any thumbnails the SSD is missing, newest first, then makes every missing thumbnail
and, at low priority, every missing 1600 px preview. For 113,000 photos that is about 1.9 GB of WebP
thumbnails and 37 GB of previews; the previews stop while the SSD has under 20 GB free. The JPEG
thumbnails made before 2026-11-20 (2.75 GB) are no longer read once each photo's WebP exists; to
reclaim the space, `find ~/Library/Caches/photo-estate/thumbs -name '*.jpg' -delete` after the log says
the thumbnails are made. To fill it by hand:

```bash
rsync -a /Volumes/estate/review/ ~/Library/Caches/photo-estate/
```

`~/Library/Caches` is outside macOS's protected folders, so writing there needs no permission.
Deleting the folder is safe; it refills from the disk.

## macOS permission for the external disk

The first time launchd starts the agent, macOS asks to let **uv** access files on a removable
volume. Until someone clicks **Allow** on the Mac's screen, the process hangs in `open()` on
`estate.db` and uses no CPU. Full Disk Access is **not** needed: the removable-volume permission
is enough, and it survives restarts.

macOS ties the permission to the exact `uv` binary, so `uv` is pinned in Homebrew
(`brew pin uv`). To upgrade: `brew unpin uv && brew upgrade uv`, restart the agent, and click
Allow once more.

If the service doesn't answer a minute after a restart, check for that dialog first:

```bash
/usr/bin/log show --last 5m --style compact --predicate 'process == "tccd"' | grep -i removable
```

(Use `/usr/bin/log`: zsh has a builtin `log` that shadows it.)

## The demo

The demo estate (`embed.py serve --demo`, ~100 CC0 photos committed in `demo/`) runs beside the
real app as a second agent, so it can be shown to anyone without opening the real library.

| | |
|---|---|
| Code | `~/src/photo-estate-demo`, a worktree of the clone above (`git worktree add`), so the demo can run a different branch than the real app |
| launchd agent | `~/Library/LaunchAgents/com.jflam.photo-search-demo.plist` ([copy](com.jflam.photo-search-demo.plist)) |
| Log | `~/Library/Logs/photo-search-demo.log` |
| Local port | `127.0.0.1:8767` |
| Tailnet | `tailscale serve --bg --https=8767 http://127.0.0.1:8767` |

It reads only the repo (the catalog is copied to a temporary folder at startup, thumbnails are
made there, and no searches are logged), so it needs no disk permission and doesn't touch
`/Volumes/estate`. It starts in a few seconds.

```bash
cd ~/src/photo-estate && git fetch && git worktree add ../photo-estate-demo origin/main   # once
cp ~/src/photo-estate-demo/photo-estate/deploy/com.jflam.photo-search-demo.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jflam.photo-search-demo.plist
tailscale serve --bg --https=8767 http://127.0.0.1:8767
```

With a default-deny tailnet policy, port 8767 needs its own grant, like 8766.

## A preview of a cloud session

Work done in a cloud session (Claude Code on the web, started from a phone) can be looked at before it
is merged. [CLOUD-SESSIONS.md](CLOUD-SESSIONS.md) describes the whole way of working; this is
the mini's part of it. A cloud machine takes no connections from outside, and its network carries no tunnel out
either (its proxy passes HTTPS and WebSocket, not the relay protocol of tailcat or Tailscale, and no
UDP), so the work leaves the session the one way it can, as a pushed branch: the mini serves the demo
app of the branch with the newest commit, and shows it on the tailnet.

```
cloud session: tools/cloud-preview.sh  --git push-->  GitHub
mini, as the user photopreview: preview-branch.sh  --git fetch, every 30 s-->  its own clone
                                 -->  embed.py serve --demo on 127.0.0.1:8768
phone  --tailnet-->  mini :8768 (tailscale serve)
```

| | |
|---|---|
| Cloud side | [`tools/cloud-preview.sh`](../tools/cloud-preview.sh): pushes the session's branch, and says what will be shown |
| Mini side | `/usr/local/photo-preview/preview-branch.sh` ([copy](preview-branch.sh)), owned by root: fetches, and restarts the app when the newest commit changes |
| Runs as | `photopreview` (uid 600): a hidden, standard user with no password and a group of its own |
| Code | `/Users/photopreview/preview`, a clone the script makes for itself |
| Search model | `/Users/photopreview/.cache/huggingface`, a copy of mine |
| LaunchDaemon | `/Library/LaunchDaemons/com.jflam.photo-preview.plist` ([copy](com.jflam.photo-preview.plist)) |
| Log | `/var/log/photo-preview/preview.log` (a line for each branch and commit it shows; anyone on the mini can read it) |
| Local port | `127.0.0.1:8768` |
| Tailnet | `tailscale serve --bg --https=8768 http://127.0.0.1:8768` |
| Open it | `https://johns-mac-mini.<tailnet>.ts.net:8768` |

"Newest" is by commit date, over every branch of the repository on GitHub, `main` included: so a
session's branch from its first push until something newer is pushed, and `main` again once the branch
is merged. Two sessions at work at once take turns; `PREVIEW_BRANCH=<name>` in the plist's environment
holds it to one branch. The preview does not depend on the session's machine, which stops between
turns.

### Its own user

The preview runs whatever is pushed to any branch of the repository, so it does not run as me. It is a
LaunchDaemon that runs as `photopreview`, which [`preview-user-setup.sh`](preview-user-setup.sh)
creates (it needs root, is safe to run again, and replaces the per-user agent this used to be):

```bash
# on the mini, once, and again after preview-branch.sh or the plist changes
sudo bash ~/src/photo-estate/photo-estate/deploy/preview-user-setup.sh
tailscale serve --bg --https=8768 http://127.0.0.1:8768
```

What keeps that user out, as checked on 2026-11-18:

- **My home folder** is `drwxr-x--- jflam:staff`, so anyone in `staff` can enter it, and macOS puts a
  standard user in `staff`. `photopreview` has a group of its own instead, and the script stops if it
  ever finds it in `staff` or `admin`.
- **`/Volumes/estate`** ignores ownership (`noowners`), so file permissions do not protect it from
  anyone. What does is the removable-volume permission ([above](#macos-permission-for-the-external-disk)):
  it was given to `uv` for my account, and `photopreview` running the same `/opt/homebrew/bin/uv` is
  refused with "Operation not permitted". Never click Allow on a removable-volume dialog for this user,
  and never give `bash`, `uv` or Python Full Disk Access: either would open the library to the preview.
- **The scripts it runs** are root's, in `/usr/local/photo-preview`: it can read them, not change them.
- **No way in:** its password is `*`, its shell is `/usr/bin/false`, and it is not in the group that
  may use SSH.

The script ends by having launchd run [`preview-isolation-check.sh`](preview-isolation-check.sh) once
as `photopreview` (not `sudo -u` from a terminal, which would lend the test the terminal's own disk
permissions). It tries to list `/Volumes/estate`, with `ls` and with `uv`'s Python, and to read my
home, `~/.ssh`, `~/Documents` and `~/.config/photo-estate`; every line must say `DENIED`. The result
stays in `/var/log/photo-preview/isolation-check.log`. Run the setup script again to repeat the check
after a macOS upgrade or a change to the disk.

### Its firewall

A separate user keeps the preview out of files, not off the network: on its own it could still make
requests to the real app on `127.0.0.1:8766`, which asks for no login there.
[`preview-firewall-setup.sh`](preview-firewall-setup.sh) closes that with rules in the macOS packet
filter (pf) that match `photopreview` only:

```bash
# on the mini, once, after preview-user-setup.sh
sudo bash ~/src/photo-estate/photo-estate/deploy/preview-firewall-setup.sh
```

| | |
|---|---|
| Rules | `/etc/pf.anchors/com.jflam.photo-preview` |
| Main ruleset | two lines at the end of `/etc/pf.conf` (as it was: `/etc/pf.conf.before-photo-preview`) |
| Loader | `/usr/local/photo-preview/firewall-load.sh`, run at boot by `/Library/LaunchDaemons/com.jflam.photo-preview-firewall.plist` |
| Log | `/var/log/photo-preview/firewall.log` |

The user may not open a connection to the loopback (except its own port 8768), to any of the mini's
own addresses (its tailnet address is routed through the tunnel, not the loopback, so the loopback rule
alone would miss it), or to a private network: the home network and the tailnet. It can still reach
the internet, which it needs for GitHub and the package index. The rules match the first packet of a
connection, so what its own server answers to Tailscale's proxy is not touched.

This turned pf on for the whole mini; macOS ships it off. The main ruleset holds only Apple's empty
anchors and this one, and no rule in it matches another user, so the real app, the demo, Tailscale and
SSH are not filtered. A macOS upgrade can put back the stock `/etc/pf.conf`: the loader adds the two
lines again at boot.

The isolation check covers this too: connections to 8766 and 8767, to port 22 on each of the mini's
addresses and to the router must be refused, and GitHub and the package index must answer.

**It fails open.** If pf is turned off (`pfctl -d`, or by other software that manages it), the block
is gone without a word, and there are a few seconds at boot before the rules load. Run the setup
script again to repeat the check; `sudo pfctl -s info` shows whether pf is on.

**What is still open.** Code in the preview can still:

- reach anything on the internet, and send there whatever it can read;
- read what every user of the mini can: `/Users/Shared`, `/opt/homebrew`, and the command lines of
  running programs (so keep secrets out of arguments);
- use the mini's CPU, memory and internal disk without limit.

The cloud environment needs no settings for this: no keys, no variables, and no hosts beyond GitHub.
With a default-deny tailnet policy, port 8768 needs its own grant, like 8766 and 8767.

## Tailscale access

With Tailscale's default allow-all policy there's nothing to do here. My tailnet policy is
default-deny, so two grants cover the Mac mini, both added 2026-11-11:

- **Photo app:** my laptops and phone can reach the Mac mini on port 8766, and nothing else on
  the Mac that wasn't already allowed.
- **SSH:** one laptop can reach the Mac mini on port 22, for deploying.

Both have tests in the policy, so a later edit that breaks them is refused on save. Pick a port
for the app that no other service on the machine uses.

## Comparing a second model (`/compare`)

The app also serves `/compare` ([chapter 10](../../docs/book/10-embeddings.md#trying-a-bigger-model-compare)):
each search ranked by the app's model and by SigLIP 2 large (384 px) side by side, unlabelled, with a vote.
It needs two things the app does not make itself:

| | |
|---|---|
| The second model's vectors | `~/photo-estate-lab/siglip2-large-384/vectors` (`--compare-vectors` in the plist), made by `tools/export_squares.py` here and `tools/embed_squares.py` on a machine with a fast GPU, about 170 MB for 83,000 photos |
| Its text side | `google/siglip2-large-patch16-384` in `~/.cache/huggingface` (3.5 GB; the agent runs with `HF_HUB_OFFLINE=1`, so download it first: `uvx --from huggingface_hub hf download google/siglip2-large-patch16-384`) |
| Votes | `votes.jsonl` beside the vectors |

The large model's text side takes about 3 GB of memory, on the GPU. It is loaded when a comparison is
started from `/compare` (**Compare models** in the Library's "..." menu; the page says it is loading, a
few seconds) and let go when the comparison is finished, or when one is left unused for 15 minutes
(`--compare-idle MINUTES`; 0 keeps it until the run ends), so it costs nothing on a day nobody compares.
A comparison's report is kept in `~/photo-estate-lab/siglip2-large-384/vectors/runs/`. Until 2026-11-20 it was loaded whole at every start and held: with the app's own model, 5.6 GB
of the mini's 16 GB, none of it visible in `ps` (use `footprint <pid>`). Measured after the change: the
app's footprint is 3.3-3.4 GB with no run under way (was 7.8 GB), GPU memory 1.5 GB, 4.6 GB during a run.
The checklist for comparing another model is [guides/COMPARE-A-MODEL.md](../../docs/guides/COMPARE-A-MODEL.md). Without the vectors
folder the app starts as usual and logs that `/compare` is off. To stop the experiment, take the two
`--compare-vectors` lines out of the plist and restart.

## Photos uploaded from the phone

The photo app accepts `PUT /api/upload/<YYYY/MM/DD/name>` and saves files into `/Volumes/estate/iphone`
(`--inbox` changes it). It never touches the catalog: `ingest-iphone.sh` does, in the same way
`pull-icloud.sh` does for iCloud, and only runs the slow stages (embeddings, faces) when something
new arrived.

Only members may use the app: upload, search, change the library (book chapter 11). `tailscale serve`
sets the `Tailscale-User-Login` header the app reads, and the app listens only on `127.0.0.1`, so
nothing else can set it from the network. The `--upload-user you@example.com` in the plist's `serve`
arguments made that login the first member and the sysadmin on the first start after members came in
(2026-11); after that members are kept on `/members` and the option is not read again (it stays in the
plist so the app still starts, and can go once that start has happened). Programs on the mini itself,
such as the task agent, are always the sysadmin. Someone new opens the app, asks to be let in, and waits
on `/members` for the sysadmin.

The **Library info** page (`/stats`, the bar-chart icon) shows what is waiting and has a
**Catalogue new photos** button that runs the script and shows its output, then **Restart to show
them** (the app reads the catalogue at startup; the restart works because launchd's `KeepAlive`
relaunches it). The same from a terminal:

```bash
RESTART=com.jflam.photo-search DB=~/estate/estate.db STORE=/Volumes/estate/store \
  INBOX=/Volumes/estate/iphone CACHE=/Volumes/estate/review ./photo-estate/ingest-iphone.sh ~/estate
```

`RESTART` restarts the app at the end, because it reads the catalog at startup. A launchd timer
(`StartInterval`, say 900 seconds) can run the same command; a second run while one is going exits at
once. `SKIP_MODELS=1` catalogues without embedding.

## Append-only: the one-off steps

Since phase 2 of the multiplayer plan (`docs/notes/MULTIPLAYER-PLAN.md`, book chapter 4) the estate only
grows: triggers in the catalog refuse deleting a photo, an occurrence, a member or the journal, and ingest
sets the user-immutable flag (`chflags uchg`) on every file it writes to a store whose root holds a file
named `.immutable`. It is deployed once, by hand, in this order: the backup comes first, because the
app's first start on this code adds triggers to the catalog (on the journal and the members), and the
first `estate.py` command adds `sharers`, `tombstones` and the rest of the triggers.

```bash
export PATH=/opt/homebrew/bin:$PATH
DB=~/estate/estate.db STORE=/Volumes/estate/store
cd ~/src/photo-estate

# 1. A catalog backup, checked, before the code moves
B=$HOME/photo-estate-lab/estate.db.before-append-only-$(date +%Y%m%d-%H%M)
sqlite3 $DB ".backup $B" && sqlite3 $B "pragma quick_check"          # must print ok
git fetch origin && git checkout <branch> && git pull --ff-only
launchctl kickstart -k gui/$(id -u)/com.jflam.photo-search             # then check /api/info and the log
cd photo-estate

# 2. Every existing photo gets the sysadmin as its sharer (safe to run again: it then adds nothing)
uv run estate.py --db $DB migrate                                      # "N photos given john ...; N photos in all"
sqlite3 $DB "select (select count(*) from assets), (select count(*) from sharers)"   # the two counts agree

# 3. The immutable flag. First on one scratch file on the same volume: it must refuse to go
f=$STORE/.tmp/uchg-test; touch $f && chflags uchg $f && ls -lO $f      # shows uchg
rm -f $f; ls -lO $f                                                    # "Operation not permitted": still there
chflags nouchg $f && rm $f                                             # and it goes once the flag is cleared
#    Then mark the store (new files are flagged from here on), and flag the files already in it. Files only:
#    a flagged directory could take no new photo.
touch $STORE/.immutable
find $STORE/?? -type f ! -flags +uchg -exec chflags uchg {} +          # minutes for 120k files on the USB disk
find $STORE/?? -type f ! -flags +uchg | head                           # prints nothing
```

Cautions:

- The disk ignores ownership, so any user of the mini can clear the flag (`chflags nouchg`), and `rm`
  run as root clears a user flag by itself (BSD `rm` does). It guards against accidents (an `rm -rf` in
  the wrong folder, as yourself), not against intent or `sudo`.
- **Tell the backup plan.** A copy that keeps the flag (`cp -p`, Finder, `rsync --fileflags` where
  rsync has one, Python's `copy2`) makes files that cannot be pruned or replaced until it is cleared.
  Try the backup tool on one flagged file first.
- To undo it: `rm $STORE/.immutable`, then `find $STORE/?? -type f -flags +uchg -exec chflags nouchg {} +`.
- The one way a photo leaves the estate is `uv run estate.py --db $DB purge <sha> --reason "..." --store
  $STORE --cache /Volumes/estate/review --cache ~/Library/Caches/photo-estate --backup-dir ~/photo-estate-lab`,
  run here by the sysadmin: it backs the catalog up, journals it, leaves a tombstone so no source brings
  it back, clears the flag and removes the file and its cached copies; then restart the app. The photo
  stays in the catalog backups until they rotate out, and in the sources (it lists them).


## Lenses: the one-off step (multiplayer phase 3)

Since phase 3 what a photo hides is per member: a hiding set hides for the members who see through its
lens (book chapter 11, "What each member sees"). The deploy needs nothing by hand until the migration:
the app's first start on this code adds four tables (`lenses`, `subscriptions`, `shown`,
`member_favorites`) and two columns (`change_sets.owner`, `lens_id`), and until the migration runs it
shows exactly what it showed before (a set with no lens hides for everyone; an owner-less album, an old
favourite and a photo with no sharer are the sysadmin's). Then, once, after a backup:

```bash
export PATH=/opt/homebrew/bin:$PATH
DB=~/estate/estate.db
cd ~/src/photo-estate/photo-estate

# 1. A catalog backup, checked (the migration writes to the catalog)
B=$HOME/photo-estate-lab/estate.db.before-lenses-$(date +%Y%m%d-%H%M)
sqlite3 $DB ".backup $B" && sqlite3 $B "pragma quick_check"          # must print ok

# 2. The hiding from before lenses becomes the lens "Estate defaults"; albums and sets become the sysadmin's;
#    the old favourites are copied to the sysadmin's own. Safe to run again: it then changes nothing.
uv run estate.py --db $DB migrate        # "sharers: 0 photos ..." then "lenses: N hiding sets put in the new lens ..."
sqlite3 $DB "select id, title, owner, published, estate_default from lenses"                     # one row: Estate defaults
sqlite3 $DB "select count(*) from change_sets where op='hide' and lens_id is null"               # 0
sqlite3 $DB "select (select count(*) from favorites), (select count(*) from member_favorites)"   # the two agree

# 3. The app keeps who sees what in memory: restart it, then check /api/info's total is what it was
launchctl kickstart -k gui/$(id -u)/com.jflam.photo-search
```

The owner's phone keeps `sync-feed.db`; another member's phone gets `sync-feed-<member id>.db` beside the
catalog the first time it asks. Nothing else changes for the owner: his new hides go to his own filter
lens ("John's hidden photos") unless he applies a set to the Estate defaults, and only he uses the app
until phase 5 (open question 3 of the plan).