#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pillow>=10.0", "pillow-heif>=0.18", "numpy", "av>=14"]
# ///
"""
video_demo.py — a copy of the demo library with a few videos and Live Photos in it, to try the app on.

  uv run tools/video_demo.py /tmp/video-demo            # make it (about a minute; needs the search model)
  uv run embed.py --db /tmp/video-demo/estate.db serve --store /tmp/video-demo/store --port 8767

The checked-in demo (demo/) has no videos and must not change, so this builds a separate library beside
it: demo/estate.db and demo/store copied, then a small "phone" folder made here and run through the same
steps as any source (ingest -> link -> the pairing `exif` ends with -> embed run):

  Videos 2026/Coast to mountains.mp4   three scenes, each a slow move over one demo photo (sea, snow, a waterfall):
                                       a search for any of them finds the video, at that scene
  Videos 2026/Lighthouse.mov           one scene, upright (portrait)
  Videos 2026/IMG_9001.JPG + .MOV      a Live Photo: a demo photo and a two-second clip of it
  Videos 2026/IMG_9002.JPG + .MOV      another

The videos are made from the demo's own public-domain photos (demo/CREDITS.md), so nothing new is
downloaded. exiftool is not needed: the two things the app takes from it for a video (its duration, and
the content identifier that marks a Live Photo's clip) are written to the `exif` table here, as exiftool
would have. tests/test_video_app.py builds the same library.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

FPS = 24
FOLDER = "Videos 2026"
# What each video shows: (file, size, [(a demo photo's group in demo/photos.json, which of the group, seconds)])
VIDEOS = (
    ("Coast to mountains.mp4", (640, 360), (("sunsets over the sea", 0, 4), ("snowy mountains", 0, 4), ("waterfalls", 0, 4))),
    ("Lighthouse.mov", (360, 640), (("lighthouses", 0, 5),)),
)
LIVE = (("IMG_9001", "sunsets over the sea", 2), ("IMG_9002", "snowy mountains", 1))


def demo_photos() -> dict[str, list[Path]]:
    """group -> its photos' files in the demo store, in photos.json's order."""
    groups = json.loads((HERE / "demo" / "photos.json").read_text())
    con = sqlite3.connect(f"file:{HERE / 'demo' / 'estate.db'}?mode=ro", uri=True)
    by_name = {fn: (sha, ext) for fn, sha, ext in con.execute(
        "SELECT o.filename, a.sha256, a.ext FROM occurrences o JOIN assets a ON a.sha256 = o.sha256")}
    con.close()
    out: dict[str, list[Path]] = {}
    for group, names in groups.items():
        for n in names:
            hit = by_name.get(n) or next((v for k, v in by_name.items() if os.path.splitext(k)[0] == os.path.splitext(n)[0]), None)
            if hit:
                out.setdefault(group, []).append(HERE / "demo" / "store" / hit[0][:2] / hit[0][2:4] / f"{hit[0]}{hit[1]}")
    return out


def pick(photos: dict[str, list[Path]], group: str, k: int) -> Path:
    """The k-th photo of a group; any group that has photos stands in for one photos.json no longer has."""
    have = photos.get(group) or next(v for v in photos.values() if v)
    return have[k % len(have)]


def scene(photo: Path, size: tuple[int, int], seconds: float):
    """Frames of a slow move across one photo: a window the shape of `size`, drifting and closing in."""
    from PIL import Image, ImageOps
    with Image.open(photo) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = size
    scale = max(w / im.width, h / im.height) * 1.25
    im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.BILINEAR)
    n = round(seconds * FPS)
    for i in range(n):
        k = i / max(1, n - 1)
        zoom = 1.25 - 0.25 * k                             # closing in: the window goes from the whole frame to 80% of it
        cw, ch = w * zoom / 1.25, h * zoom / 1.25
        x = (im.width - cw) * (0.2 + 0.6 * k)
        y = (im.height - ch) * 0.5
        yield im.resize((w, h), Image.Resampling.BILINEAR, box=(x, y, x + cw, y + ch))


def write_video(dest: Path, size: tuple[int, int], scenes) -> None:
    """An H.264 video of the scenes one after another, a key frame every second."""
    import av
    import numpy as np
    with av.open(str(dest), "w") as c:
        s = c.add_stream("libx264", rate=FPS)
        s.width, s.height, s.pix_fmt = size[0], size[1], "yuv420p"
        s.codec_context.gop_size = FPS
        s.codec_context.options = {"crf": "26", "preset": "veryfast"}
        i = 0
        for frames in scenes:
            for im in frames:
                f = av.VideoFrame.from_ndarray(np.asarray(im), format="rgb24")
                f.pts, f.time_base = i, Fraction(1, FPS)
                i += 1
                for p in s.encode(f):
                    c.mux(p)
        for p in s.encode():
            c.mux(p)


def make_source(src: Path) -> list[str]:
    """Write the "phone" folder; -> the names of the Live Photo clips in it."""
    photos = demo_photos()
    d = src / FOLDER
    d.mkdir(parents=True, exist_ok=True)
    for name, size, parts in VIDEOS:
        write_video(d / name, size, (scene(pick(photos, g, k), size, secs) for g, k, secs in parts))
    clips = []
    for stem, group, k in LIVE:
        photo = pick(photos, group, k)
        shutil.copyfile(photo, d / f"{stem}{photo.suffix.upper()}")
        write_video(d / f"{stem}.MOV", (480, 360), [scene(photo, (480, 360), 2)])
        clips.append(f"{stem}.MOV")
    return clips


def exif_rows(con: sqlite3.Connection, store: Path, clips: list[str]) -> None:
    """What exiftool would have written for the new videos: the duration, and for a Live Photo's clip the
    content identifier (estate.py live_mark)."""
    import video
    for sha, ext, fn in con.execute("SELECT a.sha256, a.ext, o.filename FROM assets a JOIN occurrences o"
                                    " ON o.sha256 = a.sha256 WHERE a.kind='video'").fetchall():
        dur = video.probe(str(store / sha[:2] / sha[2:4] / f"{sha}{ext}"))["duration"]
        data = {"QuickTime:Duration": dur}
        if fn in clips:
            data["Keys:ContentIdentifier"] = f"DEMO-{fn}"
        con.execute("INSERT OR REPLACE INTO exif(sha256, duration, fps, data) VALUES (?,?,?,?)",
                    (sha, dur, FPS, json.dumps(data)))
    con.commit()


def build(root: Path, embed: bool = True) -> Path:
    """Make the library in `root` (emptied first). -> root. With embed False the last step is left out (no
    model needed): the videos are catalogued and paired but not yet searchable or shown."""
    import estate
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copyfile(HERE / "demo" / "estate.db", root / "estate.db")
    shutil.copytree(HERE / "demo" / "store", root / "store")
    src = root / "phone"
    clips = make_source(src)
    db, store = str(root / "estate.db"), str(root / "store")
    run = lambda *args: subprocess.run(["uv", "run", "--quiet", *args], cwd=HERE, check=True,
                                       env={**os.environ, "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "1")})
    run("estate.py", "--db", db, "ingest", "--store", store, "--force", str(src))
    run("estate.py", "--db", db, "link", "--source", src.name)
    con = estate.connect(db)
    con.execute("UPDATE assets SET taken_at='2026-07-0' || (rowid % 9 + 1) || 'T10:00:00+00:00' WHERE kind='video'")
    exif_rows(con, root / "store", clips)
    print(f"{estate.pair_live(con)} Live Photo clips paired with their stills")
    con.close()
    if embed:
        run("embed.py", "--db", db, "run", "--store", store)
    return root


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = build(Path(sys.argv[1]).resolve())
    print(f"\nuv run embed.py --db {out / 'estate.db'} serve --store {out / 'store'} --port 8767")
