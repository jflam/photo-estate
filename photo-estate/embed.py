#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["torch", "transformers>=4.49", "pillow>=10.0", "pillow-heif>=0.18", "sentencepiece", "protobuf", "av>=14"]
# ///
"""
embed.py — Tier 1: SigLIP 2 image embeddings for semantic search (kept apart from estate.py so the
catalog tools don't pull in PyTorch).

  run     embed every image in the catalog that has no embedding for this model yet (resumable), then
          every video that is not a Live Photo's clip: its frames (video.py), kept in video_frames, and
          their mean as the video's own vector
  search  text query -> best-matching photos, by cosine similarity
  stats   how many images are embedded per model
  hold-prompts   the text vectors of private.py's PROMPTS, written to hold-prompts.json (the hold at ingestion
          reads them there, so it never loads the model)
  serve   the photo app (http://127.0.0.1:8766/): photo_app.PhotoApp, behind cmd_serve

  uv run embed.py --db /Volumes/estate/estate.db run --store /Volumes/estate/store
  uv run embed.py --db /Volumes/estate/estate.db search "kids at the lake"
  uv run embed.py serve --demo     # the photo app on the ~100 public-domain photos in demo/

Vectors are L2-normalised float16, one row per (sha256, model), so a better model can be added
later without touching the first. Queries must use the same model as the vectors they search.
The model runs on the Mac's GPU (MPS) when there is one (--device auto), else on the CPU; the vectors
it makes agree with the CPU's to a cosine of 0.9997 or better (queries: identical rankings), so vectors
made on either can be searched from the other.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

from core import config, events, schema
from core.media import EMBED_SKIP_EXT as SKIP_EXT   # RAW: Pillow can't decode it
from encoder import DEVICES, pick_device
from encoder import features as _features

DEFAULT_MODEL = "google/siglip2-base-patch16-256"
SCHEMA = schema.EMBEDDINGS
# A typed search shows only photos that match it, not the whole library in ranked order. Measured
# for siglip2-base-patch16-256 with the two-prompt average in encoder.py (cosine between query and
# photo), on the demo and on a 100k-photo library:
#   - a photo of the thing scores 0.085-0.16; unrelated photos stay under ~0.07, so a search for
#     something that isn't there ("kangaroo" in the demo: best 0.069) finds nothing;
#   - in a big library scores fall smoothly with no gap, and ~0.04 below the best match is where
#     results drift from the thing asked for to its surroundings ("latte art" -> plain coffee,
#     "unicorn" -> horses). Narrower drops real matches from a small library.
MATCH_FLOOR = 0.085
MATCH_WINDOW = 0.04
# Shown best first, a search's matches are no longer in order of how well they match, so the loose ones at
# the end of the list are no longer out of the way: the best of them rise to the top. Measured on "mountain"
# in a 108k-photo library (3,614 matches): photos of slot canyons and desert rock are 1-2% of the matches
# within 0.025 of the best, and 7-15% of those beyond it; best first, half of the first 60 photos came from
# the loosest third. So best first keeps only what is within this of the best match.
BEST_WINDOW = 0.025


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    schema.ensure(con, "embeddings", "video")
    return con


def _decode(job: tuple[str, str, int]):
    """Worker: open one image, upright it and shrink to the model's input size. Never raises."""
    sha, path, side = job
    try:
        from PIL import Image, ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as im:
            im.draft("RGB", (side * 2, side * 2))  # fast reduced-size JPEG decode
            im = ImageOps.exif_transpose(im).convert("RGB")
            im = im.resize((side, side), Image.Resampling.BICUBIC)  # SigLIP squashes, no crop
            return sha, im.tobytes(), None
    except Exception as e:
        return sha, None, f"{type(e).__name__}: {e}"[:300]


def _decode_video(job: tuple[str, str, int]):
    """Worker: the frames a video is searched by (video.frames), each squashed to the model's input size as
    _decode does a photo. -> (sha, [(seconds, bytes)], (width, height) upright or None, error). Never raises."""
    sha, path, side = job
    try:
        from PIL import Image
        import video
        frames = video.frames(path, side=side * 2)
        got = [(t, im.convert("RGB").resize((side, side), Image.Resampling.BICUBIC).tobytes()) for t, im in frames]
        size = None
        if frames:   # the stored size, turned the way the frames came out (a phone's portrait video is stored on its side)
            p, im = video.probe(path), frames[0][1]
            w, h = p["width"], p["height"]
            size = (w, h) if (w >= h) == (im.width >= im.height) else (h, w)
        return sha, got, size, None if got else "no frame could be decoded"
    except Exception as e:
        return sha, [], None, f"{type(e).__name__}: {e}"[:300]


def video_rows(sha: str, model: str, times: list[float], vecs, stamp: str):
    """What one video's frame vectors (unit, float32, in time order) become: the video_frames rows of the
    frames worth keeping (video.distinct) and the embeddings row, their mean made unit length again. The mean
    is what "more like this" and "Best first" see of a video; a typed search goes by its best frame."""
    import numpy as np
    import video
    keep = video.distinct(vecs)
    mean = vecs[keep].mean(axis=0)
    mean = (mean / max(float(np.linalg.norm(mean)), 1e-12)).astype(np.float16)
    frames = [(sha, model, float(times[i]), vecs[i].astype(np.float16).tobytes()) for i in keep]
    return frames, (sha, model, mean.shape[0], mean.tobytes(), None, stamp)


def load_model(mid: str, threads: int, device: str = "auto"):
    """torch, the model on its device (encoder.pick_device), the processor, and the device's name."""
    import torch   # pyright: ignore[reportMissingImports]  (the ml group: not in the dev environment)
    from transformers import AutoModel, AutoProcessor   # pyright: ignore[reportMissingImports]
    torch.set_num_threads(threads)
    dev = pick_device(torch, device)
    return torch, AutoModel.from_pretrained(mid, dtype=torch.float32).eval().to(dev), AutoProcessor.from_pretrained(mid), dev


def cmd_run(a) -> None:
    con = connect(a.db)
    store = Path(a.store)
    # Each lookup below walks the embeddings index: over the whole library that is ~10 s when it is not
    # cached and the catalog is on a hard disk, so an incremental run (--after-rowid) looks up only new assets.
    todo = con.execute(
        "SELECT a.sha256, a.ext FROM assets a WHERE a.kind='image' AND a.rowid > ? AND NOT EXISTS"
        " (SELECT 1 FROM embeddings e WHERE e.sha256=a.sha256 AND e.model=?) ORDER BY a.sha256",
        (a.after_rowid or 0, a.model)).fetchall()
    skipped = [(s, e) for s, e in todo if e in SKIP_EXT]
    todo = [(s, e) for s, e in todo if e not in SKIP_EXT][: a.limit or None]
    # Videos, but not a Live Photo's clip: its still is already in the search (estate.py pair_live).
    videos = [] if a.no_videos else con.execute(
        "SELECT a.sha256, a.ext FROM assets a WHERE a.kind='video' AND a.rowid > ? AND NOT EXISTS"
        " (SELECT 1 FROM embeddings e WHERE e.sha256=a.sha256 AND e.model=?) AND NOT EXISTS"
        " (SELECT 1 FROM live_pairs l WHERE l.clip=a.sha256) ORDER BY a.sha256",
        (a.after_rowid or 0, a.model)).fetchall()[: a.limit or None]
    done = "" if a.after_rowid is not None else \
        f"{con.execute('SELECT count(*) FROM embeddings WHERE model=?', (a.model,)).fetchone()[0]} already embedded, "
    print(f"{a.model}: {done}{len(todo)} to do ({len(skipped)} RAW files skipped)"
          + (f", then {len(videos)} videos" if videos else ""), flush=True)
    total = len(todo) + len(videos)
    events.emit_progress("embed", 0, total)
    if not total:
        return
    torch, model, proc, dev = load_model(a.model, a.threads, a.device)
    print(f"model on {dev}", flush=True)
    side = proc.image_processor.size["height"]
    import numpy as np
    from PIL import Image

    def embed_images(ims):
        """Unit vectors (float32, one row per image) for PIL images already at the model's size."""
        with torch.no_grad():
            px = proc(images=ims, return_tensors="pt")["pixel_values"].to(dev)
            v = torch.nn.functional.normalize(_features(torch, model.get_image_features(pixel_values=px)), dim=-1)
        return v.cpu().numpy()

    path_of = lambda s, e: str(store / s[:2] / s[2:4] / f"{s}{e}")
    jobs = [(s, path_of(s, e), side) for s, e in todo]
    t0 = last = time.time()
    n = errors = 0
    batch_ims, batch_shas, rows = [], [], []

    def flush():
        nonlocal batch_ims, batch_shas, rows
        if batch_ims:
            for s, vec in zip(batch_shas, embed_images(batch_ims).astype(np.float16)):
                rows.append((s, a.model, vec.shape[0], vec.tobytes(), None, now()))
        con.executemany("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?)", rows)
        con.commit()
        batch_ims, batch_shas, rows = [], [], []

    with Pool(a.workers) as pool:
        for sha, raw, err in pool.imap(_decode, jobs, chunksize=4) if jobs else ():
            n += 1
            if err:
                errors += 1
                rows.append((sha, a.model, None, None, err, now()))
            else:
                assert raw is not None   # _decode gives bytes or an error
                batch_ims.append(Image.frombytes("RGB", (side, side), raw))
                batch_shas.append(sha)
            if len(batch_ims) >= a.batch:
                flush()
            if time.time() - last >= 60:
                rate = n / (time.time() - t0)
                eta = (len(jobs) - n) / rate / 3600
                print(f"{datetime.now():%H:%M:%S} {n}/{len(jobs)} {rate:.1f} img/s,"
                      f" {errors} undecodable, ~{eta:.1f} h left", flush=True)
                events.emit_progress("embed", n, total)
                last = time.time()
        flush()
        if jobs:
            dt = time.time() - t0
            print(f"done: {n} images in {dt / 3600:.2f} h ({n / dt:.1f} img/s), {errors} undecodable", flush=True)
        # Videos: a worker reads each one's frames (seeking, so a long video costs a few dozen frames), the
        # model embeds them here, and each video is committed whole: a run that is stopped redoes at most one.
        t1 = last = time.time()
        nv = bad = nframes = kept = 0
        for sha, got, size, err in pool.imap(_decode_video, [(s, path_of(s, e), side) for s, e in videos]) if videos else ():
            nv += 1
            if got:
                ims = [Image.frombytes("RGB", (side, side), raw) for _, raw in got]
                vecs = np.concatenate([embed_images(ims[i:i + a.batch]) for i in range(0, len(ims), a.batch)])
                frames, row = video_rows(sha, a.model, [t for t, _ in got], vecs, now())
                con.execute("DELETE FROM video_frames WHERE sha256=? AND model=?", (sha, a.model))
                con.executemany("INSERT OR REPLACE INTO video_frames VALUES (?,?,?,?)", frames)
                con.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?)", row)
                if size:   # ingest reads no size from a video; the grid lays a photo out by it until its thumbnail is made
                    con.execute("UPDATE assets SET width=?, height=? WHERE sha256=? AND width IS NULL", (*size, sha))
                nframes += len(got)
                kept += len(frames)
            else:
                bad += 1
                con.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?)", (sha, a.model, None, None, err, now()))
            con.commit()
            if time.time() - last >= 60:
                rate = nv / (time.time() - t1)
                print(f"{datetime.now():%H:%M:%S} {nv}/{len(videos)} videos, {rate:.2f} a second, {bad} undecodable,"
                      f" ~{(len(videos) - nv) / rate / 3600:.1f} h left", flush=True)
                events.emit_progress("embed", len(jobs) + nv, total)
                last = time.time()
        if videos:
            print(f"done: {nv} videos in {(time.time() - t1) / 60:.1f} min, {kept} of {nframes} frames kept,"
                  f" {bad} undecodable", flush=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cmd_search(a) -> None:
    import numpy as np
    con = connect(a.db)
    rows = con.execute("SELECT sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL",
                       (a.model,)).fetchall()
    if not rows:
        sys.exit(f"no embeddings for {a.model}; run `embed.py run` first")
    shas = [r[0] for r in rows]
    M = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1).astype(np.float32)
    torch, model, proc, dev = load_model(a.model, a.threads, a.device)
    with torch.no_grad():
        ti = proc(text=[a.query.lower()], padding="max_length", max_length=64, return_tensors="pt").to(dev)
        q = torch.nn.functional.normalize(_features(torch, model.get_text_features(**ti)), dim=-1)[0].cpu().numpy()
    sims = M @ q
    top = np.argsort(-sims)[: a.limit]
    print(f"{a.query!r}: top {len(top)} of {len(rows)} images ({a.model})")
    for i in top:
        r = con.execute("SELECT a.taken_at, (SELECT dir || '/' || filename FROM occurrences o"
                        " WHERE o.sha256=a.sha256 ORDER BY id LIMIT 1) FROM assets a WHERE sha256=?",
                        (shas[i],)).fetchone()
        print(f"  {sims[i]:.3f}  {(r[0] or '')[:10]:10}  {shas[i][:12]}  {r[1]}")


def _has_photos(db: str | None, model: str) -> bool:
    if not db or not Path(db).exists():
        return False
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            return c.execute("SELECT 1 FROM embeddings WHERE model=? AND vec IS NOT NULL LIMIT 1", (model,)).fetchone() is not None
        finally:
            c.close()
    except sqlite3.Error:
        return False


def _library_or_setup(a) -> bool:
    """Where the library is: --root, else --store with --db (as before), else the one chosen on the setup
    page last time. With photos in it, fill in a.db/a.store and return True. Without (a first run), serve
    the setup page instead (intake/server.py), which becomes the full app once the first photos are in;
    return False when it stops."""
    import atexit
    import shutil
    import tempfile
    from intake.api import Library, saved_root
    from intake.server import run_setup
    explicit_db = a.db != "estate.db"
    if a.sandbox and not a.root:   # practice: a scratch library, gone when the app stops
        a.root = tempfile.mkdtemp(prefix="photo-estate-sandbox-")
    if a.sandbox and Path(a.root).name.startswith("photo-estate-sandbox-"):
        import signal
        atexit.register(shutil.rmtree, a.root, ignore_errors=True)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # stopped by a service manager: still clean up
    root = Path(a.root).expanduser().resolve() if a.root else \
        Path(a.db).resolve().parent if (a.store or explicit_db) else saved_root()
    db = a.db if explicit_db or not root else str(root / "estate.db")
    store = a.store or (str(root / "store") if root else None)
    if root and _has_photos(db, a.model):
        a.db, a.store, a.root = db, store, str(root)
        return True
    port = config.app_port(a.port, sandbox=a.sandbox)

    def app_argv(lib: Library) -> list[str]:
        argv = [sys.executable, str(Path(__file__).resolve()), "--db", lib.db, "--model", a.model,
                "--threads", str(a.threads), "--device", a.device, "serve", "--root", str(lib.root), "--store", str(lib.store), "--port", str(port)]
        return argv + (["--sandbox"] if a.sandbox else []) + (["--fast-cache", a.fast_cache] if a.fast_cache else [])

    lib = Library(root, db, store, fixed=bool(a.root and not a.sandbox) or bool(a.store) or explicit_db,
                  remember=not a.sandbox)
    run_setup(lib, port, app_argv, practice=a.sandbox)
    return False


def cmd_serve(a) -> None:
    """The photo app: describe a photo and/or narrow by place, person, album, trip, camera and date.

    With no library yet, the setup page instead (_library_or_setup). Otherwise PhotoApp (photo_app.py)
    sets the app up and wires its route modules together; this starts its background work and serves it."""
    if not a.demo and not _library_or_setup(a):
        return
    from photo_app import PhotoApp
    app = PhotoApp(a)
    app.start()
    app.serve()


def text_vectors(texts: list[str], model_id: str, threads: int = 8, device: str = "auto"):
    """Unit text vectors (float32, a row each) for these texts as written, as cmd_search makes a query's."""
    torch, model, proc, dev = load_model(model_id, threads, device)
    with torch.no_grad():
        ti = proc(text=[t.lower() for t in texts], padding="max_length", max_length=64, return_tensors="pt").to(dev)
        return torch.nn.functional.normalize(_features(torch, model.get_text_features(**ti)), dim=-1).cpu().numpy()


def cmd_hold_prompts(a) -> None:
    """Write private.VECTORS: the text vectors of private.PROMPTS for --model, which the hold at ingestion
    compares each new photo with (private.py). Made once, and again when the prompts or the model change, so
    the ingest path never loads the model. Other models' vectors in the file are kept."""
    import base64
    import json
    import private
    try:
        got = json.loads(private.VECTORS.read_text())
    except (OSError, ValueError):
        got = {}
    vecs = text_vectors(list(private.PROMPTS), a.model, a.threads, "cpu")
    got[a.model] = {p: base64.b64encode(v.astype("float16").tobytes()).decode() for p, v in zip(private.PROMPTS, vecs)}
    private.VECTORS.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    print(f"{private.VECTORS}: {len(private.PROMPTS)} prompts for {a.model}")


def cmd_stats(a) -> None:
    con = connect(a.db)
    images = con.execute("SELECT count(*) FROM assets WHERE kind='image'").fetchone()[0]
    print(f"images in catalog: {images}")
    videos, clips = con.execute("SELECT count(*), count(l.clip) FROM assets a LEFT JOIN live_pairs l ON l.clip=a.sha256"
                                " WHERE a.kind='video'").fetchone()
    frames = dict(con.execute("SELECT model, count(*) FROM video_frames GROUP BY model"))
    print(f"videos in catalog: {videos} ({clips} are Live Photo clips, searched by their stills)")
    for m, ok, bad in con.execute("SELECT model, count(vec), count(error) FROM embeddings GROUP BY model"):
        print(f"  {m}: {ok} embedded, {bad} undecodable" + (f", {frames[m]} video frames" if m in frames else ""))


def parser() -> argparse.ArgumentParser:
    """The command line. `parser().parse_args(["serve", "--demo"])` is the options PhotoApp takes."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="estate.db")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--threads", type=int, default=8, help="PyTorch threads (8 measured fastest)")
    p.add_argument("--device", choices=DEVICES, default="auto",
                   help="where the model runs: auto (the Mac's GPU when there is one), mps or cpu")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="embed images that have no embedding for --model yet")
    s.add_argument("--store", required=True)
    s.add_argument("--workers", type=int, default=3, help="decoder processes, in parallel with the model")
    s.add_argument("--batch", type=int, default=16)
    s.add_argument("--after-rowid", type=int, help="only assets added after this rowid (incremental runs: a few index lookups, not a scan)")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--no-videos", action="store_true", help="photos only: leave the videos for a later run")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("search", help="semantic search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(fn=cmd_search)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    sub.add_parser("hold-prompts", help="write the text vectors the hold at ingestion compares photos with"
                   " (private.py's PROMPTS, in hold-prompts.json)").set_defaults(fn=cmd_hold_prompts)
    s = sub.add_parser("serve", help="local search page")
    s.add_argument("--root", help="the library's folder: estate.db and store/ inside it (default: the one chosen on"
                   " the setup page; with no library yet, the app opens the setup page)")
    s.add_argument("--store", help="the photo store (default: <root>/store)")
    s.add_argument("--sandbox", action="store_true",
                   help=f"practise setting up: a scratch library and practice sources made from the demo photos (port {config.SANDBOX})")
    s.add_argument("--demo", action="store_true",
                   help="serve the ~100 public-domain photos in demo/ (--db, --store, --cache are ignored; nothing is logged)")
    s.add_argument("--port", type=int, help=f"default {config.REAL}, or {config.DEMO} with --demo so it can run beside"
                   f" the real one ({config.SANDBOX} with --sandbox)")
    s.add_argument("--cache", help="thumbnail cache dir (default: <db dir>/review, shared with estate.py review)")
    s.add_argument("--fast-cache", help="cache dir on a faster disk (e.g. the internal SSD), filled from --cache")
    s.add_argument("--no-pregenerate", action="store_true", help="don't build missing thumbnails in the background")
    s.add_argument("--agent-cmd", help="the Claude Code program that works on tasks (tasks.py); default: `claude` on PATH or in ~/.local/bin")
    s.add_argument("--agent-env", help="a file of KEY=VALUE lines put in the agent's environment, for its sign-in token; "
                                       "default ~/.config/photo-estate/agent.env")
    s.add_argument("--agent-model", help="the model the agent uses; default: Claude Code's own")
    s.add_argument("--match-floor", type=float, default=MATCH_FLOOR,
                   help="typed searches show no photo below this similarity (-1 = no floor)")
    s.add_argument("--match-window", type=float, default=MATCH_WINDOW,
                   help="...nor more than this below the best match (2 = show every photo, ranked)")
    s.add_argument("--best-window", type=float, default=BEST_WINDOW,
                   help="a described search shown best first keeps only matches within this of the best match")
    s.add_argument("--inbox", help="where phone uploads (PUT /api/upload/...) are saved; default <db dir>/iphone (none with --demo)")
    s.add_argument("--ingest-script", help="script the Library info page runs to catalogue uploads (default: ingest-iphone.sh)")
    s.add_argument("--ingest-quiet", type=float, default=60.0,
                   help="seconds without an upload before the ingest runs by itself (0 = only when asked)")
    s.add_argument("--ingest-max-wait", type=float, default=600.0,
                   help="an ingest starts at least this long after the first upload of a burst, however many follow")
    s.add_argument("--upload-user", action="append", default=[],
                   help="a Tailscale login that becomes an active member on the first start of a catalog with no"
                        " members, the first of them the sysadmin (repeatable); ignored once there are members,"
                        " who are kept on /members. Requests from this computer are always the sysadmin")
    s.add_argument("--sysadmin", help="the login that becomes the sysadmin on that first start (default: the first"
                                      " --upload-user)")
    s.add_argument("--backup-dir", help="copy the catalog and face-labels.jsonl here every --backup-every hours"
                   " (e.g. a folder on another disk than the catalog)")
    s.add_argument("--backup-every", type=float, default=24.0, help="hours between catalog backups")
    s.add_argument("--backup-keep", type=int, default=7, help="catalog backups to keep")
    s.add_argument("--compare-vectors", help="experiment: a folder of a second model's vectors (tools/embed_squares.py);"
                   " /compare then ranks each search by both models side by side")
    s.add_argument("--compare-votes", help="where /compare records which side won (default: votes.jsonl in --compare-vectors)")
    s.add_argument("--compare-idle", type=float, default=15.0,
                   help="minutes /compare's second model is kept after its last use; it loads again when /compare"
                        " is next opened (0: once loaded, kept)")
    s.add_argument("--query-log", help="where to record searches and photos opened (default: <db dir>/query-log.jsonl)")
    s.set_defaults(fn=cmd_serve)
    return p


def main() -> None:
    a = parser().parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
