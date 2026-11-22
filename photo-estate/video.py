"""
video.py — still frames out of a video (PyAV, which carries its own FFmpeg: nothing to install).

  probe(path) -> {"duration", "width", "height", "codec"}           what the container says (as stored, not turned upright); never decodes
  times(duration, every=EVERY, cap=CAP) -> [seconds]                 when to look: the middle of each stretch
  frames(path, every=EVERY, cap=CAP, side=None) -> [(seconds, Image)]   upright RGB stills, in order
  poster(path, side=None) -> Image | None                            the one still that stands for the video
  distinct(vecs, alike=ALIKE) -> [indices]                           the frames worth keeping: each unlike the last kept
  wants_copy(size, duration, ext) -> bool                            is the stored file too heavy, or of a kind no browser plays, to stream as it is
  stream_copy(src, dest) -> frames written                           a 1080p H.264 + AAC .mp4 of it, to play from (images.py keeps it in the cache)

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
# A copy to play from. Measured 2026-11-21 (docs/notes/VIDEO-PLAN.md): Safari, and AVPlayer on the phone, read a
# file in hundreds of small and abandoned requests and need about twice its bitrate of the path; the Mac mini
# gives a laptop on the same network about 168 Mbit/s through the tailnet, and a phone's 4K at 60 a second is
# 80 to 110 Mbit/s, so it stops and starts. Over COPY_OVER a video gets a copy: its short side COPY_SIDE at
# most, COPY_FPS a second at most, H.264 and AAC in an .mp4 with its index first, which every browser plays.
COPY_OVER = 20_000_000          # bits a second: 4K from a phone is 24 and up, 1080p is 8 to 16
COPY_SIDE, COPY_FPS = 1080, 30
COPY_RATE = 8_000_000           # bits a second of the copy at 1080p; half that under 1000 px
UNPLAYED = {".avi", ".wmv", ".3gp"}   # containers no browser plays: copied whatever their size


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


def wants_copy(size: int, duration: float | None, ext: str) -> bool:
    """Whether a stored video should have a copy to play from: one a browser cannot play as it is, or one
    over COPY_OVER bits a second. A video of unknown length is left as it is."""
    if ext.lower() in UNPLAYED:
        return True
    return bool(duration and duration > 0 and size * 8 / duration > COPY_OVER)


def _hw():
    """Apple's decoder and encoder where there are any (the Mac mini): about 1.3x real time for 4K at 60."""
    import av
    try:
        from av.codec.hwaccel import HWAccel, hwdevices_available
        if "videotoolbox" in hwdevices_available() and "h264_videotoolbox" in av.codecs_available:
            return HWAccel(device_type="videotoolbox", allow_software_fallback=True)
    except Exception:
        pass
    return None


def stream_copy(src: str, dest: str, side: int = COPY_SIDE, fps: float = COPY_FPS) -> int:
    """Write `dest` (.mp4): the video in `src` upright, its short side `side` px at most, `fps` a second at
    most (every other frame of a faster one), H.264 at COPY_RATE, its sound as stereo AAC, the index at the
    front. -> frames written. Raises if it cannot be read or nothing could be written; `dest` is then not
    there (it is written beside itself and moved into place)."""
    hw = _hw()
    if hw is not None:
        try:
            return _copy(src, dest, side, fps, hw)
        except Exception:      # Apple's encoder would not open (it does not in every kind of process): FFmpeg's own, slower
            pass
    return _copy(src, dest, side, fps, None)


def _in_order(frame: Any, heard: list[int]) -> Any:
    """Give a resampled sound frame its time by count: where the first one said the sound starts, then one
    sample after another. An old .wmv's sound can step back in time part way through, which an .mp4 cannot
    hold; counted, it plays on unbroken. `heard` holds the next sample's number between calls."""
    if not heard:
        heard.append(max(0, frame.pts or 0))
    frame.pts = heard[0]
    heard[0] += frame.samples
    return frame


def _copy(src: str, dest: str, side: int, fps: float, hw: Any) -> int:
    import importlib
    import os
    from fractions import Fraction
    av: Any = importlib.import_module("av")
    enc, pix = ("h264_videotoolbox", "nv12") if hw is not None else ("libx264", "yuv420p")
    with av.open(src, hwaccel=hw) as c:              # its first frame: the size and format frames come in, and which way up
        first = next(iter(c.decode(_stream(c))), None)
    if first is None:
        raise ValueError("no frame could be decoded")
    turn, w, h = _rotation(first), first.width, first.height
    k = min(1.0, side / min(w, h))
    sw, sh = max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2)
    ow, oh = (sh, sw) if turn in (90, 270) else (sw, sh)
    tmp = f"{dest}.{os.getpid()}.tmp"
    n = 0
    try:
        with av.open(src, hwaccel=hw) as inp, av.open(tmp, "w", format="mp4", options={"movflags": "+faststart"}) as out:
            vin = _stream(inp)
            vin.thread_type = "AUTO"
            ain = next((a for a in inp.streams.audio if a.codec_context is not None), None)
            rate = vin.average_rate or Fraction(30)
            step = 2 if float(rate) > fps * 1.5 else 1
            graph = av.filter.Graph()
            node = graph.add_buffer(width=w, height=h, format=first.format, time_base=vin.time_base)
            steps = [graph.add("scale", f"{sw}:{sh}")]
            if turn:
                steps.append(graph.add("transpose", {90: "clock", 270: "cclock"}[turn]) if turn != 180 else graph.add("rotate", "PI"))
            steps += [graph.add("format", pix), graph.add("buffersink")]
            for nxt in steps:
                node.link_to(nxt)
                node = nxt
            graph.configure()
            vout = out.add_stream(enc, rate=rate / step)
            vout.width, vout.height, vout.pix_fmt = ow, oh, pix
            vout.bit_rate = COPY_RATE if min(ow, oh) >= 1000 else COPY_RATE // 2
            vout.codec_context.gop_size = max(1, int(round(float(rate / step)))) * 2
            vout.codec_context.time_base = vin.time_base
            if enc == "libx264":
                vout.codec_context.options = {"preset": "veryfast"}
            aout = res = None
            if ain is not None:
                aout = out.add_stream("aac", rate=ain.codec_context.sample_rate if ain.codec_context.sample_rate in (44100, 48000) else 44100)
                aout.bit_rate, aout.layout = 128_000, "stereo"
                res = av.AudioResampler(format=aout.format, layout="stereo", rate=aout.rate)
            seen = 0
            heard: list[int] = []
            for packet in inp.demux(*([vin, ain] if ain is not None else [vin])):
                try:
                    decoded = packet.decode()
                except Exception:
                    if packet.stream is vin:
                        raise
                    continue                           # a sound packet that will not decode: the picture goes on
                if aout is not None and res is not None and packet.stream is ain:
                    for f in decoded:
                        for g in res.resample(f):
                            for p in aout.encode(_in_order(g, heard)):
                                out.mux(p)
                    continue
                for f in decoded:
                    seen += 1
                    if (seen - 1) % step:
                        continue
                    graph.push(f)
                    while True:
                        try:
                            g = graph.pull()
                        except (av.BlockingIOError, av.EOFError):
                            break
                        g.pict_type = 0                # the encoder chooses its own key frames
                        for p in vout.encode(g):
                            out.mux(p)
                        n += 1
            for p in vout.encode():
                out.mux(p)
            if aout is not None and res is not None:
                for g in res.resample(None):
                    for p in aout.encode(_in_order(g, heard)):
                        out.mux(p)
                for p in aout.encode():
                    out.mux(p)
        if not n:
            raise ValueError("no frame could be written")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return n
