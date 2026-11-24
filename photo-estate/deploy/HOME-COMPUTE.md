# Moving the estate to a Linux PC with an NVIDIA GPU (home-compute)

The estate's next home is `home-compute`: a Threadripper 1950X (16 cores, 32 threads), 62 GB of memory, an
RTX 2080 (8 GB), two 1 TB NVMe disks, Omarchy (Arch Linux). It replaces the Mac mini ([MAC-MINI.md](MAC-MINI.md)),
which keeps running the real app until the last step below. This page is what was found and set up on
2026-11-24, what the GPU does for the app, and the steps of the move that are still to do.

**Where it stands (2026-11-24):** the code runs there, on the demo library only. The real library has not moved:
its disk is still on the mini.

## The computer, as found

| | |
|---|---|
| Reach it | `ssh home-compute` over Tailscale (`100.75.32.92`) |
| OS | Omarchy (Arch), kernel 7.2, systemd 262; `sudo` asks for a password, so root steps are the owner's |
| GPU | RTX 2080, driver 615.78, CUDA 13.4; the desktop (Hyprland) holds about 1.8 GB of its 8 GB |
| Disks | `nvme0n1`: the system, btrfs under LUKS, 858 GB free. `nvme1n1`: 931 GB, one partition, no filesystem |
| Tools there | `git`, `uv` (through mise), `node`, `sqlite3`, `rsync`, `tailscale`, `claude` (through mise) |
| Not there | `exiftool` (`sudo pacman -S perl-image-exiftool`: `estate.py exif` and the intake need it); no CUDA or cuDNN system packages, and none are needed (below) |
| Code | `~/src/photo-estate`, cloned from GitHub over HTTPS (the repository is public). It has no GitHub key: it can pull, not push |

The mini and home-compute cannot open SSH to each other: the tailnet's policy allows neither direction. Both
are on the same wired network (`192.168.20.x`).

## What runs on the GPU

Nothing is installed system-wide for it. Each script's own environment brings the CUDA libraries it needs
(PyTorch's wheel, and `onnxruntime-gpu[cuda,cudnn]`); only NVIDIA's driver comes from the system.

| Work | How | Measured on home-compute |
|---|---|---|
| A typed search (`encoder.py`) | PyTorch on CUDA: `--device auto` picks it | 6 ms a query; 85 ms on its CPU; 18 ms on the mini's GPU. The vectors agree to a cosine of 1.0 |
| Embedding photos (`embed.py run`) | the same | 57 photos a second on the 104 demo photos, the model's start included; 6 on its CPU |
| Faces (`faces.py run`) | onnxruntime's CUDA provider: `--device auto` picks it | 33 photos a second on the demo photos, start included; 8 on its CPU |
| Stories (`transcribe.py`) | Parakeet's ONNX export through `onnx-asr`, in place of MLX | 9 minutes of speech in 4.4 s |
| A heavy video's copy (`video.py`) | decoded on the CPU, encoded by NVENC | 10 s of 4K at 60 in 2.1 s (3.3 s with libx264; the mini: about 1.3x real time) |

The demo library is too small to say what a whole library takes: time the first real run.
`faces.py run` decodes photos in `--workers 3` processes by default, a number chosen for the mini: with 16 cores
and the models on the GPU, decoding is what it waits for, so try `--workers 12`.

The models, fetched once by hand as the user who runs the app (the app itself never downloads one):

```bash
cd ~/src/photo-estate/photo-estate
uv run --with huggingface_hub python -c "from huggingface_hub import snapshot_download; snapshot_download('google/siglip2-base-patch16-256')"
uv run transcribe.py fetch            # 2.4 GB into ~/.cache/photo-estate/parakeet-tdt-0.6b-v2-onnx
# the face models (280 MB) are fetched by the first `faces.py run`
```

All three are there now. The second model of `/compare` (`google/siglip2-large-patch16-384`) is not.

## Try it on the demo library

```bash
cd ~/src/photo-estate/photo-estate && HF_HUB_OFFLINE=1 uv run embed.py serve --demo --port 8767
```

The log says `text encoder ready on cuda`. Tests: `uv run pytest -m "not slow"` (358 pass in 70 s), and the whole
suite with `HF_HUB_OFFLINE=1 uv run --group ml --group faces --group places pytest` (455 pass; the browser tests
skip until `uv run playwright install chromium-headless-shell`).

## The move: what is left

### 1. The disk cannot simply be plugged in

The 8 TB disk is APFS (it holds 879 GiB: the store is 598 GiB). Linux reads APFS only through third-party
drivers and should not write to it, and the app writes to the store's disk all day (uploads, thumbnails,
catalog backups). So the library has to be copied to a Linux filesystem, and the copy checked, before the app
runs here. The store's files are named by their SHA-256, so the check is exact: hash every file and compare
with its name (the short script under "Checking the archive" in
[DISK-SETUP.md](../../docs/guides/DISK-SETUP.md)).

A way that never leaves the library in one place only:

1. Give `nvme1n1` a filesystem and mount it (root; it erases that disk, which is empty). For example btrfs at
   `/mnt/estate`, as [DISK-SETUP.md](../../docs/guides/DISK-SETUP.md#linux-btrfs-my-first-setup) does it.
2. With the app still running on the mini, copy the store and the folders beside it to `/mnt/estate` over
   the network (`rsync -a`), a few hours on the wired network. It needs one of the two computers to be allowed
   to open SSH to the other: a grant in the tailnet's policy, or a key and the LAN address.
3. Stop the app on the mini. Run the copy once more (it sends only what changed), copy the catalog with
   `sqlite3 ~/estate/estate.db ".backup ..."` and the small files beside it, and check: `PRAGMA integrity_check`,
   the row counts, and every copied file's hash against its name.
4. Start the app here (the service, below) and use it. The disk on the mini is untouched, and is the way back.
5. Later, when the copy has been trusted for a while: the 8 TB disk can be moved here and formatted for Linux,
   as the store's home (and the NVMe copy becomes the second copy) or as the backup.

### 2. The service

[photo-search.service](photo-search.service) is the systemd twin of the mini's launchd agent: the same command,
with the catalog in `~/estate/`, the store's disk at `/mnt/estate`, and the thumbnails in
`~/.cache/photo-estate/thumbs`. It is not installed yet: there is nothing for it to serve.

```bash
mkdir -p ~/.config/systemd/user ~/.local/state
cp ~/src/photo-estate/photo-estate/deploy/photo-search.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now photo-search
sudo loginctl enable-linger $USER                        # once: start at boot with nobody logged in
tailscale serve --bg --https=8766 http://127.0.0.1:8766   # the tailnet's way in, as on the mini
```

Restart after new code: `systemctl --user restart photo-search`. Log: `~/.local/state/photo-search.log`.
The tailnet's policy needs the grants the mini has, for this computer: the phone and laptops to port 8766.

### 3. What the mini has that this computer does not yet

- **The estate's agent's sign-in** (`~/.config/photo-estate/agent.env` on the mini) and the member agents'
  bindings (MAC-MINI.md, phases 7 and 8).
- **The phone and every bookmark** point at `johns-mac-mini.<tailnet>.ts.net:8766`: the iPhone app's server
  address changes, or the name moves.
- **The cloud preview** (port 8768, its own user and firewall) and **the demo** (port 8767): the scripts in
  this folder are written for macOS (`launchd`, `pf`, `dscl`).
- **The second model's vectors** for `/compare` (`~/photo-estate-lab/siglip2-large-384/vectors`, 23 GB with the
  lab's other files); the service file leaves `--compare-vectors` out until they are copied.

### 4. Code that is still written for a Mac

Not ported; each fails safely or is skipped today.

- **The immutable flag on the store's files** (`estate.py`: `chflags uchg`). Linux has `chattr +i`, which only
  root can set, so an ordinary user's app cannot flag what it ingests. `tests/test_append_only.py` skips its
  flag test here. The owner had not yet set the flag on the mini's store either.
- **The setup page's folder picker** (`intake/api.py`): it lists `/Volumes` and opens a macOS dialog.
  Typing a path works.
- **`estate.py`'s hint after removing a photo** prints the mini's `launchctl` command.
