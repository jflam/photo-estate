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

## Tailscale access

With Tailscale's default allow-all policy there's nothing to do here. My tailnet policy is
default-deny, so two grants cover the Mac mini, both added 2026-11-11:

- **Photo app:** my laptops and phone can reach the Mac mini on port 8766, and nothing else on
  the Mac that wasn't already allowed.
- **SSH:** one laptop can reach the Mac mini on port 22, for deploying.

Both have tests in the policy, so a later edit that breaks them is refused on save. Pick a port
for the app that no other service on the machine uses.
