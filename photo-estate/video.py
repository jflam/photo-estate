"""
video.py — still frames out of a video (PyAV, which carries its own FFmpeg: nothing to install).

  probe(path) -> {"duration", "width", "height", "codec"}           what the container says (as stored, not turned upright); never decodes
  times(duration, every=EVERY, cap=CAP) -> [seconds]                 when to look: the middle of each stretch
  frames(path, every=EVERY, cap=CAP, side=None) -> [(seconds, Image)]   upright RGB stills, in order
  poster(path, side=None) -> Image | None                            the one still that stands for the video
  distinct(vecs, alike=ALIKE) -> [indices]                           the frames worth keeping: each unlike the last kept

A video is searched by its frames (embed.py: `video_frames`, and one mean vector in `embeddings`) and shown
by its poster (images.py). Frames are read by seeking: each one is the key frame at or before its time, so a
long 4K video costs a few dozen decoded frames, not all of them. Two times that land on the same key frame
give one frame.

`av` is imported when first used, so the modules that import this one (images.py, for the app) start without it
and a library with no videos never loads it.
"""
from __future__ import annotations

from typing import Any

EVERY = 3.0      # seconds of video per frame
CAP = 30         # frames from one video at most: a longer video is sampled more thinly
# Cosine: a frame this close to the last one kept shows nothing new. Measured on 220 of the library's videos
# (2,352 frames, docs/notes/VIDEO-PLAN.md): half of consecutive frames are above 0.955. Keeping only frames
# under 0.96 keeps 63% of them and loses 3% of the (video, search) matches all the frames find; 0.92 keeps
# 37% and loses 16%, which is too many to save 30 MB.
ALIKE = 0.96
POSTER_AT = 1.0  # seconds in: the first frame is often black, or a blur as the phone comes up


def _open(path: str) -> Any:
    import av
    return av.open(path)


def _stream(container: Any) -> Any:
    """The picture itself: the video stream FFmpeg calls the best one. Not always the first: an old .wmv
    carries its thumbnail as a tiny second "video", ahead of the real one."""
    try:
        best = container.streams.best("video")
    except Exception:
        best = None
    return best if best is not None else container.streams.video[0]


def _rotation(frame: Any) -> int:
    """Degrees to turn a decoded frame clockwise to make it upright: 0, 90, 180 or 270. A phone writes its
    video as the sensor saw it, with a display matrix beside it; PyAV (14 and later) reads that into
    `frame.rotation`, counter-clockwise (measured on the library's videos: exiftool's Rotation 90 is -90 here)."""
    try:
        return int(round(-float(getattr(frame, "rotation", 0) or 0) / 90.0)) % 4 * 90
    except (TypeError, ValueError):
        return 0


def _seconds(container: Any, stream: Any) -> float:
    if container.duration:
        return container.duration / 1_000_000
    if stream.duration and stream.time_base:
        return float(stream.duration * stream.time_base)
    return 0.0


def probe(path: str) -> dict:
    """What the file says of itself. Raises if it is not a video PyAV can open."""
    with _open(path) as c:
        v = _stream(c)
        return {"duration": _seconds(c, v), "width": v.codec_context.width, "height": v.codec_context.height,
                "codec": v.codec_context.name}


def times(duration: float, every: float = EVERY, cap: int = CAP) -> list[float]:
    """The middle of each of n equal stretches: n is one per `every` seconds, at least 1 and at most `cap`."""
    if not duration or duration <= 0:
        return [0.0]
    n = max(1, min(cap, round(duration / every)))
    return [duration * (k + 0.5) / n for k in range(n)]


def _upright(frame: Any, side: int | None):
    from PIL import Image
    im = frame.to_image()            # RGB, through swscale
    turn = _rotation(frame)
    if turn:
        im = im.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                           270: Image.Transpose.ROTATE_90}[turn])
    if side:
        im.thumbnail((side, side))
    return im


def _at(container: Any, stream: Any, t: float):
    """The first frame decoded after seeking to the key frame at or before t (None at the end of the file)."""
    container.seek(max(0, int(t * 1_000_000)), backward=True, any_frame=False)
    for frame in container.decode(stream):
        return frame
    return None


def frames(path: str, every: float = EVERY, cap: int = CAP, side: int | None = None, at: list[float] | None = None):
    """[(seconds, upright RGB Image)] at `at`, or at times(duration). A file that will not decode gives what it
    gave before it failed; one that will not open raises."""
    out, seen = [], set()
    with _open(path) as c:
        v = _stream(c)
        v.thread_type = "AUTO"
        for t in (at if at is not None else times(_seconds(c, v), every, cap)):
            try:
                f = _at(c, v, t)
            except Exception:
                break
            if f is None:
                continue
            key = f.pts if f.pts is not None else t
            if key in seen:
                continue
            seen.add(key)
            when = float(f.pts * v.time_base) if f.pts is not None and v.time_base else t
            out.append((round(max(0.0, when), 2), _upright(f, side)))
    return out


def poster(path: str, side: int | None = None):
    """The still that stands for the video in the grid: about a second in, or the first frame of a shorter one."""
    with _open(path) as c:
        v = _stream(c)
        t = POSTER_AT if _seconds(c, v) > 2 * POSTER_AT else 0.0
        f = _at(c, v, t) or (_at(c, v, 0.0) if t else None)
        return _upright(f, side) if f is not None else None


def distinct(vecs, alike: float = ALIKE) -> list[int]:
    """Of unit vectors in time order, the places of those to keep: the first, then each whose cosine with the
    last one kept is under `alike`. A talking head or a still scene comes down to one frame."""
    keep: list[int] = []
    for i, v in enumerate(vecs):
        if not keep or float(vecs[keep[-1]] @ v) < alike:
            keep.append(i)
    return keep
