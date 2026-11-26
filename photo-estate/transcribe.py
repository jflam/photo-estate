#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#   "av>=14", "numpy", "huggingface_hub",
#   "parakeet-mlx>=0.7,<0.8; sys_platform == 'darwin' and platform_machine == 'arm64'",
#   "onnx-asr>=0.12; sys_platform != 'darwin' or platform_machine != 'arm64'",
#   "onnxruntime-gpu[cuda,cudnn]>=1.21; sys_platform == 'linux' and platform_machine == 'x86_64'",
#   "onnxruntime>=1.21; (sys_platform != 'darwin' or platform_machine != 'arm64') and (sys_platform != 'linux' or platform_machine != 'x86_64')",
# ]
# ///
"""transcribe.py — a story's recording, written out, on this computer (multiplayer phase 7): NVIDIA Parakeet TDT
0.6B v2. On Apple silicon through its MLX port (parakeet-mlx); on any other computer through its ONNX export
(onnx-asr on onnxruntime), on an NVIDIA GPU when there is one, else the CPU. Nothing is sent anywhere.

  uv run transcribe.py fetch                       once, by hand, with the network: the model (about 2.5 GB) and
                                                   this script's environment
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
model from this computer only. Without it, every recording waiting is marked with how to fetch it, and `run` exits 2.

Where the model is. The MLX one is in the Hugging Face cache. The ONNX one is in a folder of its own
(~/.cache/photo-estate/parakeet-tdt-0.6b-v2-onnx): onnxruntime refuses a model whose weights file is reached
through a link out of the model's folder, which is how the Hugging Face cache keeps every file.

A long recording. The MLX port reads one in overlapping pieces itself. The ONNX model takes a recording whole, and
the memory it needs grows with the square of its length, so here one longer than CHUNK seconds is cut at the
quietest moment in the last OVERLAP seconds of each CHUNK (pieces(), below: between words, not through one) and
the pieces' words are joined.
"""
from __future__ import annotations

import argparse
import os
import platform
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
MLX = sys.platform == "darwin" and platform.machine() == "arm64"     # Apple silicon: parakeet-mlx; else onnx-asr
MODEL = "mlx-community/parakeet-tdt-0.6b-v2" if MLX else "istupakov/parakeet-tdt-0.6b-v2-onnx"
MLX_FILES = ("config.json", "model.safetensors")
ONNX_FILES = ("config.json", "vocab.txt", "encoder-model.onnx", "encoder-model.onnx.data", "decoder_joint-model.onnx")
RATE = 16000                 # what the model listens at
CHUNK, OVERLAP = 120.0, 15.0     # seconds: a long recording is read in pieces (overlapping ones on MLX: parakeet-mlx's own way)
QUIET = 0.3                  # seconds: the length of the quiet moment a long recording is cut in (ONNX)
MISSING = (f"the speech model ({MODEL}) is not on this computer: run `uv run transcribe.py fetch` once "
           "(the deploy runbook)")


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


def pieces(samples, rate: int = RATE, chunk: float = CHUNK, look: float = OVERLAP, quiet: float = QUIET) -> list:
    """`samples` as a list of pieces of at most `chunk` seconds each, in order and with nothing left out: each cut
    is in the middle of the quietest `quiet` seconds of the last `look` seconds of its piece."""
    import numpy as np
    n, back, win = int(chunk * rate), int(look * rate), max(1, int(quiet * rate))
    out, start = [], 0
    while len(samples) - start > n:
        tail = samples[start + n - back:start + n].astype(np.float64) ** 2
        loud = np.convolve(tail, np.ones(win), "valid")          # the energy of every `quiet` seconds of the tail
        cut = start + n - back + int(np.argmin(loud)) + win // 2
        out.append(samples[start:cut])
        start = cut
    out.append(samples[start:])
    return out


def onnx_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "photo-estate" / MODEL.split("/")[-1]


def fetch() -> list[str]:
    """Download the model for this computer (by hand, once, with the network). -> the files."""
    os.environ["HF_HUB_OFFLINE"] = "0"
    from huggingface_hub import hf_hub_download  # pyright: ignore[reportMissingImports]
    if MLX:
        return [hf_hub_download(MODEL, name) for name in MLX_FILES]
    return [hf_hub_download(MODEL, name, local_dir=onnx_dir()) for name in ONNX_FILES]     # real files, not links


def model() -> Callable[[Path], str]:
    """A transcriber (path -> text) with the model already on this computer only. ModelMissing without it."""
    return mlx_model() if MLX else onnx_model()


def onnx_model() -> Callable[[Path], str]:
    """Parakeet's ONNX export through onnx-asr: on an NVIDIA GPU when onnxruntime can use one, else the CPU."""
    d = onnx_dir()
    if not all((d / f).is_file() for f in ONNX_FILES):
        raise ModelMissing(MISSING)
    os.environ["HF_HUB_OFFLINE"] = "1"
    import onnx_asr  # pyright: ignore[reportMissingImports]
    import onnxruntime as ort  # pyright: ignore[reportMissingImports]
    ort.set_default_logger_severity(3)     # (it warns of two copies between the GPU and the CPU in the graph)
    want = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
    if "CUDAExecutionProvider" in want and hasattr(ort, "preload_dlls"):
        ort.preload_dlls()                 # CUDA and cuDNN from this environment's nvidia packages, not the system's
    m = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v2", d, providers=want)

    def transcribe(path: Path) -> str:
        said = (m.recognize(piece, sample_rate=RATE) for piece in pieces(decode(path)))
        return " ".join(t.strip() for t in said if t.strip())
    return transcribe


def mlx_model() -> Callable[[Path], str]:
    """Parakeet's MLX port, with the model from the Hugging Face cache."""
    from huggingface_hub import try_to_load_from_cache  # pyright: ignore[reportMissingImports]
    for f in MLX_FILES:
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
        self.failed = failed         # failed(why): the job could not run at all (no uv, no speech library on this computer)
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
    sub.add_parser("fetch", help="download the model onto this computer (by hand, once, with the network)")
    sub.add_parser("run", help="write out every recording waiting (what the app runs)")
    f = sub.add_parser("file", help="transcribe one file and print it")
    f.add_argument("path")
    a = ap.parse_args()
    if a.cmd == "fetch":
        print(*fetch(), sep="\n")
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
