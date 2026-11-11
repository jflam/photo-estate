# Storage: choosing it, preparing it, filling it

The estate needs two places (see [Where it runs](../README.md#where-it-runs) in the README):

- a **store** for the photos themselves: any directory, local or on the network
- a **local disk** for the catalog (`estate.db`) and the app's thumbnail cache

They can be the same disk. Just don't put the catalog on a network share, because SQLite's
locking isn't reliable over SMB or NFS.

Below, `ROOT` is wherever you keep the estate. My three setups are used as examples throughout.

## Choosing a filesystem

| Filesystem | Good for | Watch out for |
|---|---|---|
| **APFS** (macOS) | A disk that lives on a Mac. What I use now. | Linux can only read it. No data checksums; check by re-hashing (below). |
| **btrfs** (Linux) | A disk that lives on a Linux box. What I started with. | macOS can't read it at all, which is why mine had to be reformatted when it moved to the Mac. |
| **ext4 / XFS** (Linux) | Same as btrfs, simpler. | No data checksums. |
| **ZFS** | A NAS or server you already run ZFS on. | Setup effort. |
| **exFAT** | Moving data between Mac and Linux (I used a USB SSD this way). | No journaling: don't keep the catalog on it, and don't unplug it mid-write. |
| **A NAS share** (SMB/NFS) | A store that several machines can reach. What I'm moving to next. | Keep the catalog local. Slower ingest and embedding than a direct disk. |

The store's filenames are lowercase hex, so case-insensitive filesystems are fine.

## Preparing a disk

**Check the size and model before formatting. Formatting erases the disk.**

### macOS, APFS (my current setup)

```bash
diskutil list external physical        # find it, e.g. disk5: "One Touch", 8.0 TB
diskutil eraseDisk APFS estate GPT disk5
sudo mdutil -i off /Volumes/estate     # optional: stop Spotlight indexing 120k photos
```

It mounts at `/Volumes/estate`. A background service reading it will trigger a one-time macOS
permission dialog; see [deploy/MAC-MINI.md](deploy/MAC-MINI.md).

### Linux, btrfs (my first setup)

```bash
lsblk -o NAME,SIZE,MODEL,TRAN,FSTYPE,MOUNTPOINT   # find it, e.g. sda: 7.3T, usb
sudo wipefs -a /dev/sdX
sudo mkfs.btrfs -L estate -f /dev/sdX
sudo mkdir -p /mnt/estate
echo "LABEL=estate /mnt/estate btrfs noatime,nofail,x-systemd.device-timeout=10s 0 0" | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount /mnt/estate && sudo chown $USER: /mnt/estate
```

`nofail` means the machine still boots when the disk is unplugged. `sudo btrfs scrub start
/mnt/estate` once a month reads every block and checks it against btrfs's own checksums.

### A NAS (my next setup: a UniFi UNAS)

Make a share on the NAS, mount it on the machine that runs the app, and keep the catalog on that
machine's own disk. For example, on a Mac, connect to `smb://<nas>/photos` in Finder (tick
"Remember this password in my keychain" so it reconnects after a reboot), then:

```bash
DB=~/estate/estate.db STORE=/Volumes/photos/store ./pull-drive.sh /Volumes/photos
uv run embed.py --db ~/estate/estate.db serve --store /Volumes/photos/store
```

The thumbnail cache follows the catalog (`~/estate/review/`), so browsing never waits on the
network. A NAS with RAID survives a dead drive, but it's still one copy; see the last section.

## Moving an existing estate

The estate is just files, so moving it is a copy. When my disk went from btrfs to APFS, I had no
second disk big enough, so the data went out in two parts (a 500 GB USB SSD formatted exFAT, and
the Mac's internal SSD over SSH), the disk was erased, and both parts came back:

```bash
rsync -a --info=progress2 OLD_ROOT/ NEW_HOST:NEW_ROOT/     # GNU rsync; macOS's openrsync lacks flags
```

Before erasing anything, compare file counts and total bytes on both sides, plus the SHA-256 of
`estate.db`. Small files (the thumbnail cache) copy slowly to a hard disk; that's expected.

## Your own rclone client ID (optional, 10 min)

`pull-drive.sh` uses rclone to read Takeout zips from Google Drive. rclone's shared Google key
"will stop working during 2026"; if it's cut off partway through a large pull, the pull stops until
you do this anyway.

1. Go to https://console.cloud.google.com/, create a project, and enable the **Google Drive API**.
2. Under OAuth consent screen, choose External, add yourself as a test user, then **Publish app**.
   Publishing stops the token expiring every 7 days. No Google review is needed for personal use.
3. Under Credentials, create an OAuth client ID of type **Desktop app** and copy the ID and secret.
4. Run `rclone config update gdrive client_id <ID> client_secret <SECRET> config_refresh_token true`.
   A browser window opens; approve it.

## Pull the whole library (unattended, probably overnight)

Keep the machine awake while it runs. On Linux:

```bash
cd photo-estate && systemd-inhibit --what=sleep:idle ./pull-drive.sh ROOT 2>&1 | tee -a ROOT/pull.log
```

On macOS (with Homebrew bash, rclone and sqlite3 installed):

```bash
cd photo-estate && caffeinate -i ./pull-drive.sh ROOT 2>&1 | tee -a ROOT/pull.log
```

- It downloads each 50 GB zip to a local staging folder (`STAGING`, default `~/estate-staging`)
  while the previous one is ingested into the store, then deletes it. It never needs more than
  about 110 GB of local disk.
- Drive is only read; nothing in Google is changed.
- **Ctrl-C at any time and re-run the same command**: finished zips are skipped, and a
  half-ingested zip resumes.
- How long it takes depends on your internet speed: 428 GiB is about 2 h at 500 Mbit/s and about
  10 h at 100 Mbit/s. Ingest (hashing, plus decoding HEIC for near-dupe detection) runs in
  parallel with the next download.

At the end it links sidecars, prints stats and snapshots the catalog next to itself
(`estate.db.bak-YYYYMMDD`). `pull-icloud.sh` does the same for iCloud Photos.

Layout:

```
ROOT/
  store/ab/cd/<sha256>.<ext>   every unique file, exactly once, bytes untouched
  estate.db                    catalog: dates, GPS, albums, people, tags, search
  review/                      the app's thumbnail and preview cache
  pull.log
```

### Photos added since your Takeout

In Takeout, select Google Photos → "All photo albums included" → deselect all → tick only the
latest year (for me, **Photos from 2026**). Deliver it to Drive, then run:

```bash
PATTERN='takeout-2026111*.zip' ./pull-drive.sh ROOT
```

Change the pattern to match the new zip names. Files already in the store are de-duplicated by
hash, so overlap with earlier exports costs nothing.

## Checking the archive

Every file in the store is named by the SHA-256 of its contents, so you can find silent
corruption on any filesystem, with or without filesystem checksums. This reads everything, so
it takes about as long as copying the store (an hour or so per 500 GB on a USB hard disk):

```bash
python3 - ROOT/store <<'EOF'
import hashlib, pathlib, sys
bad = n = 0
for p in pathlib.Path(sys.argv[1]).glob("??/??/*"):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    n += 1
    if h.hexdigest() != p.name.split(".")[0]:
        bad += 1
        print("MISMATCH", p)
print(f"{n} files checked, {bad} bad")
EOF
```

## One disk is not an estate

Until there's a second copy, **the cloud services are still your backup: don't delete anything
there yet.** The store only ever grows (files are added, never changed), which makes backups easy:

- A second disk or a NAS: `rsync -a ROOT/ BACKUP/` (only new files are copied each time).
- Off site, encrypted: `restic` to Backblaze B2 is about $6/TB/month.
- Back up `estate.db` too. It's small, and it's where the metadata, places and embeddings live.
