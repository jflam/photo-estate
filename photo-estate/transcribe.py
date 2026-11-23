#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["parakeet-mlx>=0.7,<0.8", "av>=14", "numpy"]
# ///
"""transcribe.py — a story's recording, written out, on this computer (multiplayer phase 7): NVIDIA Parakeet TDT
0.6B v2 through its MLX port (parakeet-mlx), on Apple silicon. Nothing is sent anywhere.

  uv run transcribe.py fetch                       once, by hand, with the network: the model (about 2.5 GB, in the
                                                   Hugging Face cache) and this script's environment
  uv run transcribe.py --db estate.db --store STORE run    every recording with no transcript yet (a failed one again)
  uv run transcribe.py file story.m4a              one file, printed: to try it

The app runs `run` as a queued job (Job, below): when a recording is uploaded, and at start for any left over. It
need not be real time. Each recording's transcript is written once to its story (stories.transcribed: the trigger
on `stories` refuses a second), journaled story.transcribed, which wakes the agent when the question's asker
wanted the answer (listeners.py). A recording that cannot be transcribed keeps its reason (stories.transcript_error,
shown with the story) and is tried again on the next run.

Reading the audio: parakeet-mlx's own loader runs a system `ffmpeg`, which the Mac mini does not have on the app's
PATH. So the audio is decoded here with PyAV, which carries its own FFmpeg (as video.py does for videos): an iPhone's
AAC .m4a, Safari's audio/mp4, Chrome's and Firefox's audio/webm (Opus), .wav, .mp3, all to 16 kHz mono, and handed
to the model in place of its loader. No ffmpeg is needed.

The model is never fetched by a request or by the app: the app runs with HF_HUB_OFFLINE=1, and `run` reads the
model from the cache only. Without it, every recording waiting is marked with how to fetch it, and `run` exits 2.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
MODEL = "mlx-community/parakeet-tdt-0.6b-v2"
RATE = 16000                 # what the model listens at
CHUNK, OVERLAP = 120.0, 15.0     # seconds: a long recording is read in overlapping pieces (parakeet-mlx's own way)
MISSING = (f"the speech model ({MODEL}) is not on this computer: run `uv run transcribe.py fetch` once "
           "(deploy/MAC-MINI.md)")


class ModelMissing(RuntimeError):
    pass


def decode(path: Path, rate: int = RATE):
    """The recording as float32 samples at `rate`, one channel (PyAV: no system ffmpeg)."""
    import av
    import numpy as np
    out = []
    with av.open(str(path)) as c:
        if not c.streams.audio:
            raise ValueError("there is no sound in this file")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        for frame in c.decode(audio=0):
            for f in resampler.resample(frame):
                out.append(f.to_ndarray().reshape(-1))
        for f in resampler.resample(None):      # what the resampler still holds
            out.append(f.to_ndarray().reshape(-1))
    if not out:
        raise ValueError("the recording is empty")
    return np.concatenate(out).astype(np.float32) / 32768.0


def model() -> Callable[[Path], str]:
    """A transcriber (path -> text) with the model from the Hugging Face cache only. ModelMissing without it."""
    from huggingface_hub import try_to_load_from_cache  # pyright: ignore[reportMissingImports]
    for f in ("config.json", "model.safetensors"):
        if not isinstance(try_to_load_from_cache(MODEL, f), str):
            raise ModelMissing(MISSING)
    os.environ["HF_HUB_OFFLINE"] = "1"
    import mlx.core as mx  # pyright: ignore[reportMissingImports]
    import parakeet_mlx.parakeet as pk  # pyright: ignore[reportMissingImports]
    from parakeet_mlx import from_pretrained  # pyright: ignore[reportMissingImports]
    if not hasattr(pk, "load_audio"):
        raise RuntimeError("parakeet-mlx has changed: it has no load_audio to read the audio in place of")
    pk.load_audio = lambda path, rate, dtype=None: mx.array(decode(Path(path), rate))   # PyAV, not a system ffmpeg
    m = from_pretrained(MODEL)

    def transcribe(path: Path) -> str:
        return m.transcribe(path, chunk_duration=CHUNK, overlap_duration=OVERLAP).text.strip()
    return transcribe


def run(con: sqlite3.Connection, store: Path, transcribe: Callable[[Path], str] | None = None, out=print) -> dict:
    """Write out every recording waiting (stories.pending), with `transcribe` (default: the model). {done, failed}."""
    import stories
    waiting = stories.pending(con)
    if not waiting:
        out("transcribe: nothing waiting")
        return {"done": 0, "failed": 0, "missing": False}
    if transcribe is None:
        try:
            transcribe = model()
        except ModelMissing as e:
            for sid, _, _ in waiting:
                stories.transcribed(con, sid, error=str(e))
            out(f"transcribe: {e} ({len(waiting)} recordings waiting)")
            return {"done": 0, "failed": len(waiting), "missing": True}
    done = failed = 0
    for sid, sha, ext in waiting:
        path = stories.recording_path(store, sha, ext or "")
        try:
            if not path.exists():
                raise FileNotFoundError(f"the recording is not in the store ({path.name})")
            text = transcribe(path)
        except Exception as e:      # one bad recording does not stop the rest; its reason is kept
            stories.transcribed(con, sid, error=f"{type(e).__name__}: {e}")
            failed += 1
            continue
        stories.transcribed(con, sid, text=text)
        done += 1
    out(f"transcribe: {done} written out" + (f", {failed} could not be" if failed else ""))
    return {"done": done, "failed": failed, "missing": False}


class Job:
    """The app's queued job: runs `command` (this script's `run`, as a uv script with its own environment) when
    woken and something is waiting, one at a time, in a thread of its own; `then()` after each run."""

    def __init__(self, command: list[str], waiting: Callable[[], int], then: Callable[[], None] = lambda: None,
                 failed: Callable[[str], None] = lambda why: None, log=print, timeout: float = 3600):
        self.command, self.waiting, self.then, self.log, self.timeout = command, waiting, then, log, timeout
        self.failed = failed         # failed(why): the job could not run at all (no uv, no MLX on this computer)
        self.wake, self.stopping = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True, name="transcribe")

    def start(self) -> None:
        self.wake.set()                 # at start: anything left from before
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    def once(self) -> int | None:
        """Run the command if anything is waiting; its exit code, or None when nothing was."""
        if not self.waiting():
            return None
        env = {**os.environ, "HF_HUB_OFFLINE": "1"}
        try:
            p = subprocess.run(self.command, env=env, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.log(f"transcribe: could not run: {type(e).__name__}: {e}")
            self.failed(f"the transcription job could not run: {type(e).__name__}")
            return -1
        lines = (p.stdout + p.stderr).strip().splitlines()
        for line in lines[-5:]:
            self.log(line)
        if p.returncode not in (0, 2):     # 2: the model is missing, which `run` has said on each recording itself
            self.failed(f"the transcription job failed: {(lines or ['exit ' + str(p.returncode)])[-1][:300]}")
        self.then()
        return p.returncode

    def loop(self) -> None:
        while not self.stopping.is_set():
            self.wake.wait()
            self.wake.clear()
            if self.stopping.is_set():
                break
            try:
                self.once()
            except Exception as e:        # (a locked catalog): the next recording tries again
                self.log(f"transcribe: {type(e).__name__}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="estate.db")
    ap.add_argument("--store", help="where the recordings are (the estate's store)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="download the model into the Hugging Face cache (by hand, once, with the network)")
    sub.add_parser("run", help="write out every recording waiting (what the app runs)")
    f = sub.add_parser("file", help="transcribe one file and print it")
    f.add_argument("path")
    a = ap.parse_args()
    if a.cmd == "fetch":
        os.environ["HF_HUB_OFFLINE"] = "0"
        from huggingface_hub import hf_hub_download  # pyright: ignore[reportMissingImports]
        for name in ("config.json", "model.safetensors"):
            print(hf_hub_download(MODEL, name))
        return
    if a.cmd == "file":
        try:
            print(model()(Path(a.path)))
        except ModelMissing as e:
            sys.exit(str(e))
        return
    if not a.store:
        sys.exit("run takes --store: where the recordings are")
    sys.path.insert(0, str(HERE))
    from core import schema
    con = sqlite3.connect(a.db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    schema.ensure(con, "members", "stories")
    try:
        got = run(con, Path(a.store))
    finally:
        con.close()
    if got["missing"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
