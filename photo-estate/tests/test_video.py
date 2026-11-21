"""Videos and Live Photos on the server, without the search model: video.py's frames and posters (from small
videos made here with PyAV), the Live Photo pairing estate.py does after `exif` (pair_live, on catalogs
written by hand), what embed.py keeps of a video's frames, how catalog.py loads videos, clips and frames
(and an incremental load agreeing with a full one), images.py's posters and Images.video_file, and the
photo app in process: kind=/is: filters, `live` on a still, and GET /video/<sha> with Range over a real
socket. tests/test_video_app.py does the same end to end with the model.

  uv run pytest tests/test_video.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sqlite3
import struct
import sys
import threading
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent          # photo-estate/
sys.path.insert(0, str(ROOT))
import catalog  # noqa: E402
import embed  # noqa: E402
import estate  # noqa: E402
import images  # noqa: E402
import video  # noqa: E402
from core import media, schema  # noqa: E402
from images import Images  # noqa: E402

MODEL = "google/siglip2-base-patch16-256"     # the demo library's embeddings
FPS = 10


# -- small videos, made here -------------------------------------------------------------------------

def colour(second: int) -> tuple[int, int, int]:
    """What a test video shows during its `second`-th second: each second a colour of its own."""
    return (20 + 25 * second, 110, 235 - 25 * second)


def write_video(dest: Path, seconds: float, size=(64, 48), key_every: int = FPS, paint=None) -> Path:
    """An H.264 video at FPS frames a second, a key frame every `key_every` frames (and nowhere else), each
    frame `paint(frame number)` as an RGB array (default: the colour of its second)."""
    import av
    w, h = size
    if paint is None:
        def paint(i):
            return np.full((h, w, 3), colour(i // FPS), np.uint8)
    with av.open(str(dest), "w") as c:
        s = c.add_stream("libx264", rate=FPS)
        s.width, s.height, s.pix_fmt = w, h, "yuv420p"
        s.codec_context.gop_size = key_every
        s.codec_context.options = {"preset": "ultrafast",
                                   "x264-params": f"keyint={key_every}:min-keyint={key_every}:scenecut=0"}
        for i in range(round(seconds * FPS)):
            f = av.VideoFrame.from_ndarray(paint(i), format="rgb24")
            f.pts, f.time_base = i, Fraction(1, FPS)
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode():
            c.mux(p)
    return dest


def turn_quarter(src: Path, dest: Path) -> Path:
    """A copy of an MP4 whose track says "show me turned 90 degrees clockwise" (a phone held upright): the
    display matrix in the tkhd box, as a phone writes it (exiftool's Rotation 90)."""
    b = bytearray(src.read_bytes())
    i = b.find(b"tkhd")
    version = b[i + 4]
    at = i + 8 + (32 if version == 1 else 20) + 16       # past the times, track id, duration, layer, volume
    assert struct.unpack(">9i", b[at:at + 36]) == (0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    b[at:at + 36] = struct.pack(">9i", 0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000)
    dest.write_bytes(bytes(b))
    return dest


def near(px, want, tol=14) -> bool:
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(px, want))


def middle(im: Image.Image):
    return im.getpixel((im.width // 2, im.height // 2))


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> SimpleNamespace:
    d = tmp_path_factory.mktemp("videos")

    def half_red(i):          # stored landscape: the left half red, the right half black
        a = np.zeros((32, 64, 3), np.uint8)
        a[:, :32] = (255, 0, 0)
        return a
    bad = d / "bad.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypmp42 this is not a video at all" * 20)
    return SimpleNamespace(
        nine=write_video(d / "nine.mp4", 9),                          # a key frame every second
        short=write_video(d / "short.mp4", 1.5),
        flat=write_video(d / "flat.mp4", 6, key_every=1000),         # one key frame, at the start
        turned=turn_quarter(write_video(d / "landscape.mp4", 2, (64, 32), paint=half_red), d / "turned.mp4"),
        bad=bad)


# -- video.times -------------------------------------------------------------------------------------

def test_times_are_the_middles_of_equal_stretches():
    assert video.times(9) == [1.5, 4.5, 7.5]                 # one per 3 s
    assert video.times(6) == [1.5, 4.5]
    assert video.times(1) == [0.5] and video.times(2) == [1.0]   # at least one, in the middle
    assert video.times(3.3) == pytest.approx([1.65])


def test_times_cap_and_zero_durations():
    long = video.times(3600)
    assert len(long) == video.CAP == 30
    assert long == pytest.approx([3600 * (k + 0.5) / 30 for k in range(30)])
    assert len(video.times(100, cap=5)) == 5 and len(video.times(100, every=10)) == 10
    for nothing in (0, 0.0, None, -4):
        assert video.times(nothing) == [0.0], nothing      # type: ignore[arg-type]


# -- video.frames / poster / probe -------------------------------------------------------------------

def test_frames_are_upright_rgb_in_time_order(clips):
    """Each time seeks to the key frame at or before it: 1.5, 4.5, 7.5 s give the frames at 1, 4 and 7 s,
    each showing what the video shows then."""
    got = video.frames(str(clips.nine))
    assert [t for t, _ in got] == [1.0, 4.0, 7.0]
    for t, im in got:
        assert im.mode == "RGB" and im.size == (64, 48)
        assert near(middle(im), colour(int(t))), (t, middle(im))


def test_frames_at_and_side(clips):
    got = video.frames(str(clips.nine), at=[2.5, 5.2, 8.9])
    assert [t for t, _ in got] == [2.0, 5.0, 8.0]
    assert all(near(middle(im), colour(int(t))) for t, im in got)
    small = video.frames(str(clips.nine), side=32)
    assert [im.size for _, im in small] == [(32, 24)] * 3
    assert video.frames(str(clips.nine), at=[]) == []


def test_two_times_on_one_key_frame_give_one_frame(clips):
    """A video with one key frame: every seek lands on it, and it is kept once."""
    got = video.frames(str(clips.flat))
    assert len(video.times(6)) == 2 and [t for t, _ in got] == [0.0]
    assert [t for t, _ in video.frames(str(clips.flat), at=[0.5, 2.0, 5.5])] == [0.0]


def test_poster_is_a_second_in_or_the_first_frame_of_a_short_video(clips):
    p = video.poster(str(clips.nine))
    assert p is not None and p.mode == "RGB" and p.size == (64, 48) and near(middle(p), colour(1))
    s = video.poster(str(clips.short))                     # 1.5 s: not more than 2 * POSTER_AT
    assert s is not None and near(middle(s), colour(0))
    small = video.poster(str(clips.nine), side=16)
    assert small is not None and max(small.size) == 16


def test_probe(clips):
    p = video.probe(str(clips.nine))
    assert p["duration"] == pytest.approx(9.0, abs=0.15)
    assert (p["width"], p["height"], p["codec"]) == (64, 48, "h264")
    assert video.probe(str(clips.short))["duration"] == pytest.approx(1.5, abs=0.15)


def test_a_file_that_is_not_a_video_raises(clips, tmp_path):
    for fn in (video.probe, video.frames, video.poster):
        with pytest.raises(Exception):
            fn(str(clips.bad))
        with pytest.raises(Exception):
            fn(str(tmp_path / "missing.mp4"))


def test_a_turned_video_comes_out_upright(clips):
    """Stored landscape (64x32, left half red) with a display matrix of 90 degrees clockwise: PyAV reports
    rotation -90, and the frames and the poster are portrait (32x64) with the red half on top."""
    import av
    with av.open(str(clips.turned)) as c:
        assert next(c.decode(video=0)).rotation == -90
    p = video.poster(str(clips.turned))
    assert p is not None
    for im in [im for _, im in video.frames(str(clips.turned))] + [p]:
        assert im.size == (32, 64)
        assert near(im.getpixel((16, 8)), (255, 0, 0), 40) and near(im.getpixel((16, 56)), (0, 0, 0), 40)
    # what the container says is as stored, not turned (probe's docstring)
    assert (video.probe(str(clips.turned))["width"], video.probe(str(clips.turned))["height"]) == (64, 32)


@pytest.mark.parametrize("rotation,turn", [(0, 0), (-90, 90), (90, 270), (180, 180), (-180, 180), (-270, 270),
                                           (270, 90), (-89.6, 90), (None, 0), ("garbage", 0)])
def test_rotation_reading(rotation, turn):
    assert video._rotation(SimpleNamespace(rotation=rotation)) == turn
    assert video._rotation(object()) == 0                   # an old PyAV: no attribute at all


# -- video.distinct ----------------------------------------------------------------------------------

def unit_at(deg: float, dim: int = 4) -> np.ndarray:
    v = np.zeros(dim, np.float32)
    v[0], v[1] = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return v


def test_distinct_compares_with_the_last_one_kept():
    """A slow drift (10 degrees a frame, each frame 0.985 like the one before) is kept once it has moved
    far enough from the frame last kept, not from the frame before it."""
    vecs = np.stack([unit_at(a) for a in (0, 10, 20, 30, 40, 50, 60)])
    assert video.distinct(vecs, alike=0.92) == [0, 3, 6]     # cos 30 deg = 0.866 < 0.92; cos 20 deg = 0.94 is not
    assert video.distinct(vecs, alike=0.99) == list(range(7))
    assert video.distinct(np.stack([unit_at(0)] * 5)) == [0]
    assert video.distinct(np.zeros((0, 4), np.float32)) == []
    assert video.distinct(np.stack([unit_at(0), unit_at(90), unit_at(0)])) == [0, 1, 2]


def test_distinct_default_is_alike():
    """The default threshold is the constant: a frame just under ALIKE is kept, one just over is not."""
    import math
    deg = math.degrees(math.acos(video.ALIKE))
    assert video.distinct(np.stack([unit_at(0), unit_at(deg + 0.5)])) == [0, 1]
    assert video.distinct(np.stack([unit_at(0), unit_at(deg - 0.5)])) == [0]


def test_distinct_threshold_is_strict():
    v = np.array([[1.0, 0.0], [0.5, np.sqrt(0.75)]], np.float32)     # cosine 0.5
    assert video.distinct(v, alike=0.5) == [0]                       # 0.5 is not under 0.5
    assert video.distinct(v, alike=0.5001) == [0, 1]


# -- embed.py: what a video's frames become ----------------------------------------------------------

def test_video_rows():
    vecs = np.stack([unit_at(0, 8), unit_at(5, 8), unit_at(90, 8), unit_at(92, 8), unit_at(180 + 45, 8)])
    times = [1.0, 4.0, 7.0, 10.0, 13.0]
    frames, row = embed.video_rows("ab" * 32, MODEL, times, vecs, "2026-11-21T00:00:00")
    keep = video.distinct(vecs)
    assert keep == [0, 2, 4]
    assert [(f[0], f[1], f[2]) for f in frames] == [("ab" * 32, MODEL, t) for t in (1.0, 7.0, 13.0)]
    for f, i in zip(frames, keep):
        assert len(f[3]) == 8 * 2 and np.array_equal(np.frombuffer(f[3], np.float16), vecs[i].astype(np.float16))
    sha, model, dim, blob, err, stamp = row
    assert (sha, model, dim, err, stamp) == ("ab" * 32, MODEL, 8, None, "2026-11-21T00:00:00")
    mean = np.frombuffer(blob, np.float16).astype(np.float32)
    want = vecs[keep].mean(axis=0)
    want /= np.linalg.norm(want)
    assert len(blob) == 16 and np.allclose(mean, want, atol=2e-3) and abs(np.linalg.norm(mean) - 1) < 2e-3


def test_decode_video(clips, tmp_path):
    """-> (sha, [(seconds, side x side RGB bytes)], the stored size turned upright, None); never raises."""
    sha, got, size, err = embed._decode_video(("v" * 64, str(clips.nine), 16))
    assert sha == "v" * 64 and err is None and size == (64, 48)
    assert [t for t, _ in got] == [t for t, _ in video.frames(str(clips.nine))]
    for t, raw in got:
        assert len(raw) == 16 * 16 * 3
        im = Image.frombytes("RGB", (16, 16), raw)
        assert near(middle(im), colour(int(t)))
    _, got, size, err = embed._decode_video(("t" * 64, str(clips.turned), 16))
    assert err is None and len(got) == 1 and size == (32, 64), "stored 64x32 on its side: upright it is portrait"
    for path in (clips.bad, tmp_path / "missing.mp4"):
        sha, got, size, err = embed._decode_video(("x" * 64, str(path), 16))
        assert got == [] and size is None and isinstance(err, str) and err


# -- estate.py: Live Photo pairs ---------------------------------------------------------------------

ID = {"QuickTime:Duration": 2.1, "Keys:ContentIdentifier": "5A0F-1C2B"}


def new_library(tmp_path) -> sqlite3.Connection:
    return estate.connect(str(tmp_path / "estate.db"))


def put(con, fn, d="DCIM/100APPLE", source="phone", dur=None, data=None) -> str:
    """One file of a source: its asset (once per sha), its occurrence, and for a video what exiftool reads."""
    sha = hashlib.sha256(f"{source}|{d}|{fn}".encode()).hexdigest()
    kind = media.kind_of(fn)
    con.execute("INSERT OR IGNORE INTO assets(sha256, ext, kind) VALUES (?,?,?)",
                (sha, Path(fn).suffix.lower(), kind))
    con.execute("INSERT INTO occurrences(sha256, source, path, dir, filename) VALUES (?,?,?,?,?)",
                (sha, source, fn if d in ("", ".") else f"{d}/{fn}", d, fn))
    if kind == "video":
        con.execute("INSERT OR REPLACE INTO exif(sha256, duration, data) VALUES (?,?,?)",
                    (sha, dur, json.dumps(data) if isinstance(data, dict) else data))
    con.commit()
    return sha


def pairs(con) -> dict[str, tuple[str, str]]:
    return {c: (s, h) for c, s, h in con.execute("SELECT clip, still, how FROM live_pairs")}


def test_live_stems():
    assert estate.live_stems("IMG_1.MOV") == ["img_1"]
    assert estate.live_stems("IMG_1_HEVC.MOV") == ["img_1_hevc", "img_1"]
    assert estate.live_stems("IMG_1904(1).MP4") == ["img_1904(1)", "img_1904"]
    assert estate.live_stems("MVIMG_20190101_1.mp4") == ["mvimg_20190101_1"]


def test_live_mark():
    assert estate.live_mark("IMG_1.MOV", 2.0, json.dumps(ID)) == "id"
    assert estate.live_mark("IMG_1.MOV", 2.0, json.dumps({"Keys:ContentIdentifier": ""})) is None   # empty: no mark
    assert estate.live_mark("IMG_1.MOV", 2.0, '{"ContentIdentifier": not json') is None
    assert estate.live_mark("IMG_1.MOV", 2.0, json.dumps({"QuickTime:Duration": 2})) is None
    assert estate.live_mark("IMG_1.MOV", 2.0, None) is None
    assert estate.live_mark("MVIMG_1.MP4", 3.0, None) == "mvimg"
    assert estate.live_mark("mvimg_1.mp4", estate.MVIMG_LONGEST, None) == "mvimg"
    assert estate.live_mark("MVIMG_1.MP4", 4.5, None) is None and estate.live_mark("MVIMG_1.MP4", None, None) is None
    assert estate.live_mark("VID_1.MP4", 3.0, None) is None


def test_a_clip_with_the_identifier_pairs_with_its_still(tmp_path):
    con = new_library(tmp_path)
    still = put(con, "IMG_0001.HEIC")
    clip = put(con, "IMG_0001.MOV", dur=2.1, data=ID)
    assert estate.pair_live(con) == 1
    assert pairs(con) == {clip: (still, "id")}


def test_a_clip_whose_still_comes_later_pairs_on_a_later_run(tmp_path):
    """Measured: 2 clips of 16,174 had the identifier and no still. They stay videos until the still arrives."""
    con = new_library(tmp_path)
    clip = put(con, "IMG_0002.MOV", dur=2.1, data=ID)
    assert estate.pair_live(con) == 0 and pairs(con) == {}
    still = put(con, "IMG_0002.JPG")
    assert estate.pair_live(con) == 1 and pairs(con) == {clip: (still, "id")}


def test_ordinary_videos_beside_photos_do_not_pair(tmp_path):
    """An old camera's IMG_0140.MOV beside an unrelated IMG_0140.JPG: no identifier, so not a clip, whether
    it runs 20 s or 2 s."""
    con = new_library(tmp_path)
    put(con, "IMG_0140.JPG")
    put(con, "IMG_0140.MOV", dur=20.0, data={"QuickTime:Duration": 20.0})
    put(con, "IMG_0141.JPG")
    put(con, "IMG_0141.MOV", dur=2.0, data={"QuickTime:Duration": 2.0})
    assert estate.pair_live(con) == 0 and pairs(con) == {}


def test_a_long_video_with_the_identifier_is_not_considered(tmp_path):
    # The code's rule (LIVE_LONGEST): only videos up to 6 s are read for the marks, identifier or not.
    con = new_library(tmp_path)
    put(con, "IMG_0003.HEIC")
    put(con, "IMG_0003.MOV", dur=estate.LIVE_LONGEST + 1, data=ID)
    put(con, "IMG_0004.HEIC")
    edge = put(con, "IMG_0004.MOV", dur=estate.LIVE_LONGEST, data=ID)
    assert estate.pair_live(con) == 1 and list(pairs(con)) == [edge]


def test_hevc_suffix(tmp_path):
    con = new_library(tmp_path)
    still = put(con, "IMG_0005.HEIC")
    clip = put(con, "IMG_0005_HEVC.MOV", dur=2.0, data=ID)
    assert estate.pair_live(con) == 1 and pairs(con) == {clip: (still, "id")}


def test_a_numbered_clip_takes_its_own_still_else_the_base_name(tmp_path):
    con = new_library(tmp_path)
    own = put(con, "IMG_1904(1).HEIC", d="A")
    put(con, "IMG_1904.HEIC", d="A")
    clip = put(con, "IMG_1904(1).MP4", d="A", dur=2.0, data=ID)
    other = put(con, "IMG_1904(1).MP4", d="B", source="takeout", dur=2.0, data=ID)
    base = put(con, "IMG_1904.HEIC", d="B", source="takeout")
    assert estate.pair_live(con) == 2
    assert pairs(con) == {clip: (own, "id"), other: (base, "id")}


def test_two_clips_of_one_name_take_one_still_each(tmp_path):
    """Takeout: IMG_1904.JPG and IMG_1904.HEIC, with clips IMG_1904.MP4 and IMG_1904(1).MP4."""
    con = new_library(tmp_path)
    jpg, heic = put(con, "IMG_1904.JPG"), put(con, "IMG_1904.HEIC")
    first = put(con, "IMG_1904(1).MP4", dur=2.0, data=ID)     # written first: the order of the rows is no guide
    second = put(con, "IMG_1904.MP4", dur=2.0, data=ID)
    assert estate.pair_live(con) == 2
    got = pairs(con)
    assert {got[first][0], got[second][0]} == {jpg, heic}


def test_google_motion_photos(tmp_path):
    con = new_library(tmp_path)
    still = put(con, "MVIMG_20190704_1.jpg")
    clip = put(con, "MVIMG_20190704_1.MP4", dur=3.0, data={"QuickTime:Duration": 3.0})
    put(con, "MVIMG_20190704_2.jpg")
    put(con, "MVIMG_20190704_2.MP4", dur=5.0, data={"QuickTime:Duration": 5.0})    # over MVIMG_LONGEST
    put(con, "MVIMG_20190704_3.jpg")
    put(con, "MVIMG_20190704_3.MP4", dur=9.0, data={"QuickTime:Duration": 9.0})
    assert estate.pair_live(con) == 1 and pairs(con) == {clip: (still, "mvimg")}


def test_a_still_in_another_folder_does_not_pair(tmp_path):
    con = new_library(tmp_path)
    put(con, "IMG_0006.HEIC", d="DCIM/101APPLE")                 # another folder
    put(con, "IMG_0006.HEIC", d="DCIM/100APPLE/sub")              # a folder inside it
    put(con, "IMG_0006.HEIC", d="DCIM", source="icloud")          # another folder of another source
    put(con, "IMG_0006.MOV", dur=2.0, data=ID)
    assert estate.pair_live(con) == 0


def test_a_still_in_the_same_folder_of_another_source_pairs(tmp_path):
    """A Takeout is many zips, each its own source, with one folder's files spread over them."""
    con = new_library(tmp_path)
    clip = put(con, "IMG_0011.MP4", d="Photos from 2019", source="takeout-001.zip", dur=2.0, data=ID)
    still = put(con, "IMG_0011.HEIC", d="Photos from 2019", source="takeout-002.zip")
    assert estate.pair_live(con) == 1 and pairs(con) == {clip: (still, "id")}


def test_the_clips_own_source_is_looked_in_first(tmp_path):
    con = new_library(tmp_path)
    put(con, "IMG_0012.HEIC", source="a-other")                   # sorts first, and comes first in the table
    clip = put(con, "IMG_0012.MOV", source="phone", dur=2.0, data=ID)
    own = put(con, "IMG_0012.HEIC", source="phone")
    con.executemany("INSERT INTO sources(name) VALUES (?)", [("a-other",), ("phone",)])
    con.commit()
    assert estate.pair_live(con) == 1 and pairs(con) == {clip: (own, "id")}


def test_a_folder_named_dot(tmp_path):
    """A source with its files at the top: dir '.', path the bare name."""
    con = new_library(tmp_path)
    still = put(con, "IMG_0007.JPG", d=".")
    clip = put(con, "IMG_0007.MOV", d=".", dur=2.0, data=ID)
    put(con, "IMG_0008.JPG", d="")
    clip2 = put(con, "IMG_0008.MOV", d="", dur=2.0, data=ID)
    assert estate.pair_live(con) == 2 and pairs(con)[clip] == (still, "id") and clip2 in pairs(con)


def test_pairing_again_changes_nothing_and_redo_rebuilds(tmp_path):
    con = new_library(tmp_path)
    put(con, "IMG_0009.HEIC")
    put(con, "IMG_0009.MOV", dur=2.0, data=ID)
    put(con, "MVIMG_9.jpg")
    put(con, "MVIMG_9.mp4", dur=2.0)
    assert estate.pair_live(con) == 2
    before = sorted(con.execute("SELECT * FROM live_pairs"))
    assert estate.pair_live(con) == 0 and sorted(con.execute("SELECT * FROM live_pairs")) == before
    con.execute("UPDATE live_pairs SET how='stale'")
    con.commit()
    assert estate.pair_live(con, redo=True) == 2
    assert sorted((c, s, h) for c, s, h, _ in con.execute("SELECT * FROM live_pairs")) == \
        sorted((c, s, h) for c, s, h, _ in before)


def test_cmd_live(tmp_path, capsys):
    con = new_library(tmp_path)
    put(con, "IMG_0010.HEIC")
    put(con, "IMG_0010.MOV", dur=2.0, data=ID)
    put(con, "Holiday.mp4", dur=40.0)
    con.close()
    estate.cmd_live(SimpleNamespace(db=str(tmp_path / "estate.db"), redo=False))
    assert capsys.readouterr().out.strip() == \
        "1 Live Photo clips paired now; 1 of 2 videos are clips, of 1 stills; 1 videos stand on their own"
    estate.cmd_live(SimpleNamespace(db=str(tmp_path / "estate.db"), redo=False))
    assert capsys.readouterr().out.startswith("0 Live Photo clips paired now; 1 of 2")


# -- catalog.py: videos, clips and frames ------------------------------------------------------------

def sha_of(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


V1, V2, V3, V4 = sha_of("video one"), sha_of("video two"), sha_of("video three"), sha_of("video four")
C1, C2, C3 = sha_of("clip one .mov"), sha_of("clip two .mp4"), sha_of("clip three")


def unit16(v) -> bytes:
    v = np.asarray(v, np.float32)
    return (v / np.linalg.norm(v)).astype(np.float16).tobytes()


class Library:
    """A copy of the demo catalog to add videos to by hand."""

    def __init__(self, tmp_path: Path):
        self.tmp, self.db = tmp_path, tmp_path / "estate.db"
        shutil.copyfile(ROOT / "demo" / "estate.db", self.db)       # never write to demo/
        con = self.con()
        self.dim = con.execute("SELECT dim FROM embeddings WHERE model=? LIMIT 1", (MODEL,)).fetchone()[0]
        self.photos = [s for s, in con.execute("SELECT sha256 FROM assets WHERE kind='image' ORDER BY sha256")]
        con.close()
        self.rng = np.random.default_rng(7)

    def con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db)
        schema.ensure(con, "video")
        return con

    def random(self) -> np.ndarray:
        return self.rng.normal(size=self.dim).astype(np.float32)

    def vec_of(self, sha) -> bytes:
        con = self.con()
        v = con.execute("SELECT vec FROM embeddings WHERE sha256=? AND model=?", (sha, MODEL)).fetchone()[0]
        con.close()
        return v

    def taken_of(self, sha) -> str:
        con = self.con()
        t = con.execute("SELECT taken_at FROM assets WHERE sha256=?", (sha,)).fetchone()[0]
        con.close()
        return t

    def video(self, sha, ext=".mp4", dur=12.34, vec: bytes | None = None, frames=(), taken="2021-06-01T10:00:00+00:00",
              kind="video", name=None):
        con = self.con()
        con.execute("INSERT INTO assets(sha256, ext, kind, taken_at, width, height) VALUES (?,?,?,?,?,?)",
                    (sha, ext, kind, taken, 640, 360))
        fn = name or f"{sha[:8]}{ext}"
        con.execute("INSERT INTO occurrences(sha256, source, path, dir, filename) VALUES (?,?,?,?,?)",
                    (sha, "phone", f"Videos/{fn}", "Videos", fn))
        con.execute("INSERT INTO exif(sha256, duration, data) VALUES (?,?,?)", (sha, dur, "{}"))
        if vec is not None:
            con.execute("INSERT INTO embeddings VALUES (?,?,?,?,?,?)", (sha, MODEL, self.dim, vec, None, "now"))
        con.executemany("INSERT INTO video_frames VALUES (?,?,?,?)", [(sha, MODEL, t, v) for t, v in frames])
        con.commit()
        con.close()

    def pair(self, clip, still):
        con = self.con()
        con.execute("INSERT INTO live_pairs VALUES (?,?,?,?)", (clip, still, "id", "now"))
        con.commit()
        con.close()

    def load(self, prev=None) -> catalog.Catalog:
        cache = self.tmp / "cache"
        return catalog.load(sqlite3.connect(self.db), MODEL, cache, cache, prev, demo=True)


@pytest.fixture
def lib(tmp_path) -> Library:
    return Library(tmp_path)


def stocked(lib: Library):
    """Two videos, two clips of one still (a .mov and an .mp4), and stray frames. -> (query, still)."""
    q = np.frombuffer(unit16(lib.random()), np.float16).astype(np.float32)     # exactly a frame's vector
    lib.video(V1, vec=unit16(lib.random()), dur=12.34, frames=[
        (1.0, unit16(lib.random())), (4.0, q.astype(np.float16).tobytes()), (7.0, unit16(lib.random())),
        (9.0, np.zeros(lib.dim - 1, np.float16).tobytes()),          # the wrong length: left out
    ], name="Beach day.mp4")
    lib.video(V2, ext=".mov", vec=unit16(lib.random()), dur=None)
    con = lib.con()
    con.execute("INSERT INTO video_frames VALUES (?,?,?,?)", (V2, "another/model", 0.5, q.astype(np.float16).tobytes()))
    con.commit()
    con.close()
    still = lib.photos[3]
    lib.video(C1, ext=".mov", dur=2.0, vec=unit16(lib.random()), frames=[(1.0, q.astype(np.float16).tobytes())])
    lib.video(C2, ext=".mp4", dur=2.0)
    lib.pair(C1, still)
    lib.pair(C2, still)
    return q, still


def test_videos_load_like_photos(lib):
    q, still = stocked(lib)
    c = lib.load()
    for v, dur in ((V1, 12.3), (V2, None)):
        assert v in c.index and c.meta[v]["kind"] == "video" and c.meta[v]["dur"] == dur   # pyright: ignore
        assert c.video[c.index[v]]
    assert c.meta[V1]["path"] == "Videos/Beach day.mp4"
    assert all("kind" not in c.meta[s] and "dur" not in c.meta[s] and not c.video[c.index[s]] for s in lib.photos)
    assert c.info["videos"] == 2 and c.info["total"] == len(lib.photos) + 2
    assert set(c.meta[s].get("live", "") for s in lib.photos) == {"", C2}
    assert c.meta[still]["live"] == C2, "a still with two clips carries the .mp4"                # pyright: ignore
    assert C1 not in c.index and C2 not in c.index and C1 not in c.meta and C2 not in c.meta
    assert c.clips == {C1: ".mov", C2: ".mp4"}
    json.dumps(c.info)
    json.dumps([c.feed_row(s) for s in c.shas])


def test_frames_are_loaded_and_strays_dropped(lib):
    """V1's three good frames; not its frame of the wrong length, nor V2's for another model, nor the frame
    of C1, a clip (embedded before it was found to be one), which is in no list."""
    q, _ = stocked(lib)
    c = lib.load()
    assert c.F.shape == (3, lib.dim) and c.F.dtype == np.float32
    assert c.frame_of.tolist() == [c.index[V1]] * 3 and c.frame_t.tolist() == [1.0, 4.0, 7.0]
    assert np.array_equal(c.F[1], q)


def test_a_video_is_scored_by_its_best_frame(lib):
    q, _ = stocked(lib)
    c = lib.load()
    s = c.sims(q)
    i = c.index[V1]
    assert s[i] == pytest.approx(max(float(c.M[i] @ q), float(np.max(c.F @ q))))
    assert s[i] == pytest.approx(float(q @ q)) and s[i] > float(c.M[i] @ q) + 0.5
    others = np.arange(c.N) != i
    assert np.array_equal(s[others], (c.M @ q)[others]), "photos, and a video with no frames, score as before"
    away = -q                                                # a vector the mean is nearer than any frame
    assert c.sims(away)[i] == pytest.approx(max(float(c.M[i] @ away), float(np.max(c.F @ away))))
    assert c.moment(V1, q) == 4.0
    assert c.moment(V2, q) is None and c.moment(lib.photos[0], q) is None and c.moment("0" * 64, q) is None


def test_a_video_is_never_in_a_stack(lib):
    """A video with the same vector as a photo, taken the same second: a photo there would be stacked with it."""
    p = lib.photos[10]
    lib.video(V3, vec=lib.vec_of(p), taken=lib.taken_of(p), kind="video")
    c = lib.load()
    assert c.stack_of[c.index[V3]] == -1 and c.stack_n[c.index[V3]] <= 1 and not c.dup[c.index[V3]]
    assert c.stack_of[c.index[p]] == -1 and c.stack_n[c.index[p]] <= 1
    con = lib.con()
    con.execute("UPDATE assets SET kind='image', ext='.jpg' WHERE sha256=?", (V3,))
    con.commit()
    con.close()
    twin = lib.load()
    assert twin.stack_of[twin.index[V3]] >= 0 and twin.stack_of[twin.index[V3]] == twin.stack_of[twin.index[p]]


def test_incremental_load_with_videos_matches_a_full_one(lib):
    stocked(lib)
    first = lib.load()
    still = lib.photos[20]
    lib.video(V3, vec=unit16(lib.random()), frames=[(0.5, unit16(lib.random())), (3.5, unit16(lib.random()))])
    lib.video(C3, ext=".mov", dur=1.8)
    lib.pair(C3, still)
    again = lib.load(prev=first.raw)
    assert again.differences(lib.load()) == ""
    assert again.meta[still]["live"] == C3 and V3 in again.index and len(again.F) == 5     # pyright: ignore
    assert again.frame_of.tolist().count(again.index[V3]) == 2
    nothing = lib.load(prev=again.raw)                       # nothing new: the frames are not read again
    assert nothing.differences(lib.load()) == "" and nothing.raw["frames"][3] is again.raw["frames"][3]


def test_a_video_later_found_to_be_a_clip_leaves_the_catalog_on_an_incremental_load(lib):
    """The order pair_live allows: a clip whose still has not arrived is a video, so `embed.py run` embeds it
    (mean and frames); then the still is ingested and a later pairing makes it a clip."""
    lib.video(V4, ext=".mov", dur=2.0, vec=unit16(lib.random()), frames=[(1.0, unit16(lib.random()))])
    first = lib.load()
    assert V4 in first.index
    lib.video(sha_of("its still"), ext=".jpg", kind="image", vec=unit16(lib.random()))
    lib.pair(V4, sha_of("its still"))
    again = lib.load(prev=first.raw)
    full = lib.load()
    assert V4 not in full.index
    assert again.differences(full) == ""


# -- images.py ---------------------------------------------------------------------------------------

def stored(store: Path, sha: str, ext: str, src: Path) -> Path:
    dest = store / sha[:2] / sha[2:4] / f"{sha}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest


def test_upright_and_make_for_a_video(clips, tmp_path):
    im = images.upright(clips.nine, 360)
    assert im.mode == "RGB" and im.size == (64, 48) and near(middle(im), colour(1))
    assert images.upright(clips.turned, 360).size == (32, 64)
    with pytest.raises(Exception):
        images.upright(clips.bad, 360)
    thumb, preview = tmp_path / "t.webp", tmp_path / "p.jpg"
    assert images._make((str(clips.nine), str(thumb), str(preview)))
    assert thumb.read_bytes()[8:12] == b"WEBP" and preview.read_bytes()[:2] == b"\xff\xd8"
    assert images._make((str(clips.bad), str(tmp_path / "b.webp"), None)) is False


def test_video_file(clips, tmp_path):
    store = tmp_path / "store"
    vid, clip, photo, gone = sha_of("v"), sha_of("c"), sha_of("p"), sha_of("gone")
    src = stored(store, vid, ".mp4", clips.nine)
    csrc = stored(store, clip, ".mov", clips.short)
    cat = SimpleNamespace(meta={vid: {"ext": ".mp4", "kind": "video", "path": "Videos 2026/Beach day.mp4"},
                                gone: {"ext": ".mp4", "kind": "video", "path": "x/gone.mp4"},
                                photo: {"ext": ".jpg", "path": "x/p.jpg"}},
                          clips={clip: ".mov"})
    im = Images(SimpleNamespace(cat=cat), store, tmp_path / "cache", tmp_path / "cache")
    assert im.video_file(vid) == (src, "video/mp4", "Beach day.mp4")
    assert im.video_file(clip) == (csrc, "video/mp4", f"{clip[:12]}.mov")
    for nope in (photo, gone, "../../etc/passwd", "ab", vid.upper() + "x", sha_of("unknown")):
        assert im.video_file(nope) is None, nope
    # a video's thumbnail and preview are its poster; it has no /original
    status, body, ctype, _ = im.handle("GET", f"/thumb/{vid}", {}, None)
    assert (status, ctype, body[8:12]) == (200, "image/webp", b"WEBP")
    status, body, ctype, _ = im.handle("GET", f"/preview/{vid}", {}, None)
    assert (status, ctype, body[:2]) == (200, "image/jpeg", b"\xff\xd8")
    assert im.handle("GET", f"/original/{vid}", {}, None)[0] == 404


# -- the photo app, in process (no model) ------------------------------------------------------------

@pytest.fixture(scope="module")
def app(tmp_path_factory, clips):
    """The demo catalog with one video (nine seconds, embedded, three frames) and a Live Photo clip of one of
    its photos, served from a store holding just those two files."""
    from photo_app import PhotoApp
    tmp = tmp_path_factory.mktemp("app")
    lib = Library(tmp)
    store = tmp / "store"
    vid_bytes, clip_bytes = clips.nine.read_bytes(), clips.short.read_bytes()
    vid, clip = hashlib.sha256(vid_bytes).hexdigest(), hashlib.sha256(clip_bytes).hexdigest()
    stored(store, vid, ".mp4", clips.nine)
    stored(store, clip, ".mov", clips.short)
    lib.video(vid, vec=unit16(lib.random()), dur=9.0, frames=[(t, unit16(lib.random())) for t in (1.0, 4.0, 7.0)],
              name="Nine seconds.mp4")
    lib.video(clip, ext=".mov", dur=1.5)
    still = lib.photos[5]
    lib.pair(clip, still)
    a = PhotoApp(embed.parser().parse_args(["--db", str(lib.db), "serve", "--store", str(store), "--no-pregenerate"]))
    a.feed_update()
    return SimpleNamespace(app=a, vid=vid, clip=clip, still=still, photo=lib.photos[0], size=len(vid_bytes),
                           bytes=vid_bytes, n=len(lib.photos) + 1)


def results(app, q: str) -> list[dict]:
    r = app.request("GET", f"/api/search?dedupe=0&limit=500&{q}")
    assert r.status == 200, r.body
    return r.json()["results"]


def test_kind_and_is_filters(app):
    a = app.app
    assert a.request("GET", "/api/info").json()["videos"] == 1
    got = results(a, "kind=video")
    assert [r["sha"] for r in got] == [app.vid] and got[0]["kind"] == "video" and got[0]["dur"] == 9.0
    photos = results(a, "kind=photo")
    assert len(photos) == app.n - 1 and not any("kind" in r for r in photos)
    assert [r["sha"] for r in results(a, "q=is:video")] == [app.vid]
    assert a.request("GET", "/api/search?q=is:videos").json()["filters"] == ["is:videos"]
    assert app.vid not in {r["sha"] for r in results(a, "q=is:photo")} and len(results(a, "q=is:photo")) == app.n - 1
    assert len(results(a, "kind=anything")) == app.n, "an unknown kind is no filter"
    assert results(a, "kind=video&q=is:photo") == []


def test_a_stack_with_a_live_photo_in_it_says_which_clip_it_plays(lib):
    """Two shots of one moment, the second a Live Photo: both rows of the stack name its clip (`stack_live`),
    so the tile on top can play it. A Live Photo on its own has `live` and no `stack_live`."""
    p, alone = lib.photos[10], lib.photos[3]
    lib.video(V3, ext=".jpg", vec=lib.vec_of(p), taken=lib.taken_of(p), kind="image", dur=None)
    lib.video(C1, ext=".mov", dur=2.0)
    lib.video(C2, ext=".mov", dur=2.0)
    lib.pair(C1, V3)
    lib.pair(C2, alone)
    c = lib.load()
    g = int(c.stack_of[c.index[p]])
    assert g >= 0 and g == c.stack_of[c.index[V3]] and c.stack_live == {g: C1}
    rows, _, _ = c.take(np.array([c.index[p], c.index[V3], c.index[alone]]), 0, 10, False)
    assert [r.get("stack_live") for r in rows] == [C1, C1, None]
    assert [r.get("live") for r in rows] == [None, C1, C2]


def test_stills_carry_their_clip_and_clips_are_in_no_list(app):
    every = results(app.app, "")
    assert len(every) == app.n and app.clip not in {r["sha"] for r in every}
    assert next(r for r in every if r["sha"] == app.still)["live"] == app.clip
    assert sum("live" in r for r in every) == 1
    rows = app.app.request("GET", "/api/sync?since=0&limit=10000").json()["photos"]
    by = {r["sha"]: r for r in rows}
    assert app.clip not in by and by[app.still]["live"] == app.clip
    assert (by[app.vid]["kind"], by[app.vid]["dur"]) == ("video", 9.0)


def test_similar_to_a_video(app):
    r = app.app.request("GET", f"/api/similar?sha={app.vid}&limit=5")
    assert r.status == 200 and len(r.json()["results"]) == 5 and app.vid not in {x["sha"] for x in r.json()["results"]}


def test_video_route_in_process(app):
    a = app.app
    r = a.request("GET", f"/video/{app.vid}")
    assert (r.status, r.content_type) == (501, "video/mp4")       # streamed by the HTTP layer, not here
    assert a.request("GET", f"/video/{app.clip}").status == 501
    assert a.request("GET", f"/video/{app.photo}").status == 404
    assert a.request("GET", f"/video/{'0' * 64}").status == 404


@pytest.fixture(scope="module")
def port(app):
    srv = app.app.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port: int, path: str, *headers: str) -> tuple[int, dict, bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n".encode()
                  + "".join(f"{h}\r\n" for h in headers).encode() + b"\r\n")
        got = b""
        while chunk := s.recv(65536):
            got += chunk
    top, _, body = got.partition(b"\r\n\r\n")
    lines = top.decode().split("\r\n")
    head = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return int(lines[0].split()[1]), head, body


def test_video_over_http_with_ranges(app, port):
    size, data = app.size, app.bytes
    code, h, body = get(port, f"/video/{app.vid}")
    assert (code, h["content-type"], h["accept-ranges"], int(h["content-length"])) == (200, "video/mp4", "bytes", size)
    assert body == data and "content-range" not in h and "immutable" in h["cache-control"]
    assert h["content-disposition"] == "inline; filename*=UTF-8''Nine%20seconds.mp4"
    code, h, body = get(port, f"/video/{app.vid}", "Range: bytes=0-1")
    assert (code, h["content-range"], h["content-length"], body) == (206, f"bytes 0-1/{size}", "2", data[:2])
    code, h, body = get(port, f"/video/{app.vid}", "Range: bytes=-100")
    assert (code, h["content-range"], body) == (206, f"bytes {size - 100}-{size - 1}/{size}", data[-100:])
    code, h, body = get(port, f"/video/{app.vid}", "Range: bytes=1000-1499")
    assert (code, body) == (206, data[1000:1500])
    code, h, body = get(port, f"/video/{app.vid}", f"Range: bytes={size - 10}-{size + 5000}")
    assert (code, h["content-range"], body) == (206, f"bytes {size - 10}-{size - 1}/{size}", data[-10:])
    code, h, body = get(port, f"/video/{app.vid}", f"Range: bytes={size}-")
    assert (code, h["content-range"]) == (416, f"bytes */{size}")
    code, h, body = get(port, f"/video/{app.vid}", "Range: bytes=0-1,5-6")      # several: the whole file
    assert (code, len(body)) == (200, size)
    code, h, body = get(port, f"/video/{app.clip}")
    assert (code, h["content-type"], body[4:8]) == (200, "video/mp4", b"ftyp")
    assert get(port, f"/video/{app.photo}")[0] == 404
    assert get(port, f"/original/{app.vid}")[0] == 404
    code, h, body = get(port, f"/thumb/{app.vid}")
    assert (code, h["content-type"], body[8:12]) == (200, "image/webp", b"WEBP")
    code, h, body = get(port, f"/preview/{app.vid}")
    assert (code, h["content-type"], body[:2]) == (200, "image/jpeg", b"\xff\xd8")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
