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
was btrfs (see `../DISK-SETUP.md`), which macOS can't read. It was reformatted after copying
everything off and back, verified by file count, byte total and the `estate.db` SHA-256.

Spotlight is turned off for the volume (`sudo mdutil -i off /Volumes/estate`). The app has its own
search, and indexing 120k photos slows every big copy.

## The service

| | |
|---|---|
| Code | `~/src/photo-estate` (a clone of this repo) |
| launchd agent | `~/Library/LaunchAgents/com.jflam.photo-search.plist` (copy in this folder) |
| Log | `~/Library/Logs/photo-search.log` |
| Query log | `/Volumes/estate/query-log.jsonl` (next to the catalog) |
| Catalog backups | `/Volumes/estate/catalog-backups/` (`--backup-dir`): the app copies `estate.db` and `face-labels.jsonl` there daily and keeps 7 |
| Local port | `127.0.0.1:8766` |
| Tailnet | `tailscale serve --bg --https=8766 http://127.0.0.1:8766` |

It starts at login and restarts if it exits. The page answers about 15 seconds after a restart,
most of it reading the catalog off the USB disk; typed searches wait about 5 seconds more for the
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
views on the internal SSD (about 2.5 GB). Anything missing there is copied from
`/Volumes/estate/review` on first use, or decoded from the original if neither has it. At startup
the server copies any thumbnails the SSD is missing, newest first. To fill it by hand:

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
mini: preview-branch.sh  --git fetch, every 30 s-->  its own clone  -->  embed.py serve --demo on 127.0.0.1:8768
phone  --tailnet-->  mini :8768 (tailscale serve)
```

| | |
|---|---|
| Cloud side | [`tools/cloud-preview.sh`](../tools/cloud-preview.sh): pushes the session's branch, and says what will be shown |
| Mini side | `~/.config/photo-estate/preview-branch.sh` ([copy](preview-branch.sh)): fetches, and restarts the app when the newest commit changes |
| Code | `~/Library/Caches/photo-estate-preview`, a clone the script makes for itself |
| launchd agent | `~/Library/LaunchAgents/com.jflam.photo-preview.plist` ([copy](com.jflam.photo-preview.plist)) |
| Log | `~/Library/Logs/photo-preview.log` (a line for each branch and commit it shows) |
| Local port | `127.0.0.1:8768` |
| Tailnet | `tailscale serve --bg --https=8768 http://127.0.0.1:8768` |
| Open it | `https://johns-mac-mini.<tailnet>.ts.net:8768` |

"Newest" is by commit date, over every branch of the repository on GitHub, `main` included: so a
session's branch from its first push until something newer is pushed, and `main` again once the branch
is merged. Two sessions at work at once take turns; `PREVIEW_BRANCH=<name>` in the plist's environment
holds it to one branch. The preview does not depend on the session's machine, which stops between
turns, and it runs whatever is pushed to a branch of the repository, so it is as trusted as push access
to the repository is. Only the demo library is ever served: the real app and `/Volumes/estate` are not
involved.

```bash
# on the mini, once
mkdir -p ~/.config/photo-estate
cp ~/src/photo-estate/photo-estate/deploy/preview-branch.sh ~/.config/photo-estate/
cp ~/src/photo-estate/photo-estate/deploy/com.jflam.photo-preview.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jflam.photo-preview.plist
tailscale serve --bg --https=8768 http://127.0.0.1:8768
```

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

## Photos uploaded from the phone

The photo app accepts `PUT /api/upload/<YYYY/MM/DD/name>` and saves files into `/Volumes/estate/iphone`
(`--inbox` changes it). It never touches the catalog: `ingest-iphone.sh` does, in the same way
`pull-icloud.sh` does for iCloud, and only runs the slow stages (embeddings, faces) when something
new arrived.

To accept uploads only from your own login, add `--upload-user you@example.com` (repeatable) to the
`serve` arguments in the plist. `tailscale serve` sets the `Tailscale-User-Login` header the app
checks, and the app listens only on `127.0.0.1`, so nothing else can set it from the network.

The **Library info** page (`/stats`, the bar-chart icon) shows what is waiting and has a
**Catalogue new photos** button that runs the script and shows its output, then **Restart to show
them** (the app reads the catalogue at startup; the restart works because launchd's `KeepAlive`
relaunches it). The same from a terminal:

```bash
RESTART=com.jflam.photo-search ./photo-estate/ingest-iphone.sh /Volumes/estate
```

`RESTART` restarts the app at the end, because it reads the catalog at startup. A launchd timer
(`StartInterval`, say 900 seconds) can run the same command; a second run while one is going exits at
once. `SKIP_MODELS=1` catalogues without embedding.
