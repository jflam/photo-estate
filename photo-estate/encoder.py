"""
encoder.py — the SigLIP 2 model as the photo app (`embed.py serve`) holds it: its text side, for typed
searches (browse.py), and with `images` its image side too, for photos arriving from the phone (admin.py).

  Encoder(model_id, threads, images, device, idle)   .start() loads it in the background, .load() in this thread
  .ensure()                            start loading it if it is not loaded, loading or failed (a model kept only while used)
  .let_go()                            free the model; .ensure() loads it again
  .state                               its core.lifecycle.Lifecycle: loading, then ready or failed (idle again once let go)
  .wait(timeout)                       return once loaded; raise ModelLoading (503) or ModelFailed (500)
  .text(q)                             a query's unit vector (waits for the model, up to .patience seconds)
  .image(im)                           a PIL image's float16 vector, as `embed.py run` makes it
  features(torch, out)                 the vector in what a model call returns (embed.py uses it too)
  pick_device(torch, choice)           "auto" -> "cuda" with an NVIDIA GPU, "mps" on a Mac with a GPU, else "cpu"
                                       (embed.py uses it too)
  empty_cache(torch, dev)              hand a GPU's unused memory back

Importing PyTorch and loading the model is most of the app's startup time, and only typed queries need
it, so it loads while the page, filters and thumbnails are already being served. A search that comes
first waits for it, but only WAIT seconds (it holds the catalog gate): then ModelLoading, which the app
answers 503. A load that raises ends the wait too: every later call raises ModelFailed with the error,
which the app answers 500 (photo_app.PhotoApp.handle; estate_mcp.py makes either one a tool error).

The model runs on the GPU when there is one (CUDA on a PC with an NVIDIA card, MPS on a Mac): on the M1
Mac mini a query's text pass takes 18 ms there against 47 ms on the CPU (2.6x; docs/book/10-embeddings.md).
Only the model moves: the photos' vectors stay in NumPy, and a query vector comes back to the CPU before
browse.py compares it with them. The model is float32 on every device, so a photo's vector does not depend
on which computer made it beyond the last bits.

What it costs in memory. On a Mac the GPU's memory is the machine's own, and it is not in a process's
RSS: on the Mac mini (16 GB) the app's two models, whole and in float32, were 5.6 GB of "IOAccelerator"
memory in a 7.8 GB footprint (`footprint <pid>`), half the machine. So:
  - without `images`, the image half of the model is dropped before the model goes to the device: a
    typed search needs only the text half;
  - with `idle` seconds, a model nothing has used for that long is let go, and the next .ensure() loads
    it again (the second model of /compare, compare.py: an experiment should not hold memory all day).
"""
from __future__ import annotations

import gc
import threading
import time

from core.lifecycle import Lifecycle, LoadFailed, Phase, StillLoading

WAIT = 90.0   # seconds a search waits for the model to finish loading (a client gives up at about 120)
IMPORTING = threading.Lock()   # importing transformers from two threads at once fails: two models may load at once now


class ModelLoading(StillLoading):
    pass


class ModelFailed(LoadFailed):
    pass


def features(torch, out):
    return out if torch.is_tensor(out) else out.pooler_output


DEVICES = ("auto", "cuda", "mps", "cpu")


def drop_image_side(model) -> bool:
    """Take the image half off a two-tower model (SigLIP: .vision_model), leaving what get_text_features uses.
    True if there was one to take."""
    if getattr(model, "vision_model", None) is None:
        return False
    model.vision_model = None
    return True


def pick_device(torch, choice: str = "auto") -> str:
    """The PyTorch device for --device: "auto" is an NVIDIA GPU (CUDA) when PyTorch can use one, else the
    Mac's (MPS), else the CPU."""
    if choice not in DEVICES:
        raise ValueError(f"--device must be one of {', '.join(DEVICES)}, not {choice!r}")
    if choice == "auto":
        if torch.cuda.is_available():
            return "cuda"
        return "mps" if torch.backends.mps.is_available() else "cpu"
    return choice


def empty_cache(torch, dev: str | None) -> None:
    """Hand the GPU memory PyTorch holds for its next tensor back (a model let go frees nothing otherwise)."""
    if dev == "mps":
        torch.mps.empty_cache()
    elif dev == "cuda":
        torch.cuda.empty_cache()


class Encoder:
    def __init__(self, model_id: str, threads: int, images: bool = False, device: str = "auto",
                 idle: float | None = None):
        self.model_id, self.threads, self.want_images, self.device = model_id, threads, images, device
        self.idle, self.used = idle, time.time()   # seconds unused after which the model is let go (None: kept)
        self.parts = {}                      # torch, model, dev, tok, and images (the image processor); {} when not loaded
        self.starting = threading.Lock()     # ensure(): one of two requests starts the load
        self.state, self.lock = Lifecycle(f"the search model ({model_id})"), threading.Lock()   # one forward pass at a time
        self.patience = WAIT   # how long text(), side and image() wait for a load in flight

    def start(self) -> None:
        self.state.begin()   # here, not in the thread: a request from now on waits instead of finding it idle
        threading.Thread(target=self._load, daemon=True).start()

    def load(self) -> None:
        self.state.begin()
        self._load()

    def ensure(self) -> bool:
        """Start loading the model in the background if it is idle: never loaded, or let go. True if this
        call started it. (One that is loading, loaded or failed is left as it is.)"""
        with self.starting:
            if self.state.snapshot().phase is not Phase.IDLE:
                return False
            self.start()
            return True

    def let_go(self, unused_for: float = 0.0) -> bool:
        """Free the model if it is loaded and nothing has used it for `unused_for` seconds. True if it is
        not loaded when this returns. A forward pass in flight finishes first (self.lock)."""
        with self.lock:
            if self.state.snapshot().phase is not Phase.READY:
                return True
            if time.time() - self.used < unused_for:
                return False
            parts, self.parts = self.parts, {}
            self.state.release()
        torch, dev = parts.get("torch"), parts.get("dev")
        parts.clear()
        gc.collect()
        if torch is not None:
            empty_cache(torch, dev)          # hand the GPU's memory back, not keep it for the next tensor
        print(f"text encoder {self.model_id} let go: not used for {unused_for / 60:.0f} min", flush=True)
        return True

    def _watch(self) -> None:
        """Let the model go once it has been idle for self.idle seconds; ends when it has."""
        idle = self.idle or 0.0
        while not self.let_go(idle):
            time.sleep(max(0.05, min(60.0, self.used + idle - time.time())))

    def _open(self) -> dict:
        with IMPORTING:
            import torch   # pyright: ignore[reportMissingImports]  (the ml group: not in the dev environment)
            from transformers import AutoImageProcessor, AutoModel, AutoTokenizer   # pyright: ignore[reportMissingImports]
        torch.set_num_threads(self.threads)
        dev = pick_device(torch, self.device)
        model = AutoModel.from_pretrained(self.model_id, dtype=torch.float32).eval()
        if not self.want_images:   # before it goes to the device: the image half is most of a SigLIP model
            drop_image_side(model)
        parts = dict(torch=torch, dev=dev, tok=AutoTokenizer.from_pretrained(self.model_id), model=model.to(dev))
        if self.want_images:   # the image side, for photos arriving from the phone
            parts["images"] = AutoImageProcessor.from_pretrained(self.model_id)
        return parts

    def _load(self) -> None:
        t = time.time()
        try:
            self.parts = self._open()
        except BaseException as e:   # a missing model, no PyTorch, out of memory: every wait ends, with the error
            self.state.fail(e)
            print(f"text encoder failed to load: {self.state.snapshot().error}", flush=True)
            return
        self.state.succeed()
        try:
            self.text("warm-up")  # the first forward pass is ~1 s slower; take it here, not on a user's query
        except Exception as e:
            print(f"text encoder warm-up: {type(e).__name__}: {e}", flush=True)
        print(f"text encoder ready on {self.parts.get('dev')} in {time.time() - t:.1f}s", flush=True)
        self.used = time.time()
        if self.idle:
            threading.Thread(target=self._watch, daemon=True).start()

    def _held(self) -> dict:
        """The loaded parts, under self.lock: ModelLoading if the model was let go since the caller waited."""
        if "model" not in self.parts:
            raise ModelLoading(f"the search model ({self.model_id}) is not loaded just now; try again in a moment")
        self.used = time.time()
        return self.parts

    def wait(self, timeout: float | None) -> None:
        """Return once the model is loaded: ModelLoading if it isn't by `timeout` seconds (None: until the
        load ends; an encoder never started raises at once), ModelFailed if it failed to load."""
        self.state.require(timeout, ModelLoading, ModelFailed)

    def text(self, q: str):
        import numpy as np
        # average the raw query with a "a photo of ..." prompt: SigLIP was trained on captions
        prompts = [q.lower(), f"a photo of {q.lower()}"]
        self.wait(self.patience)
        with self.lock:
            parts = self._held()
            torch, model, dev = parts["torch"], parts["model"], parts["dev"]
            with torch.no_grad():
                ti = parts["tok"](prompts, padding="max_length", max_length=64, return_tensors="pt").to(dev)
                v = torch.nn.functional.normalize(features(torch, model.get_text_features(**ti)), dim=-1).mean(0)
        v = v.cpu().numpy()
        return v / np.linalg.norm(v)

    @property
    def side(self) -> int:
        """The image side's input size, in pixels (waits for the model)."""
        self.wait(self.patience)
        with self.lock:
            return self._held()["images"].size["height"]

    def image(self, small):
        """The vector for a photo already squashed to side x side, as `embed.py run` computes it."""
        import numpy as np
        self.wait(self.patience)
        with self.lock:
            parts = self._held()
            torch, model, dev = parts["torch"], parts["model"], parts["dev"]
            with torch.no_grad():
                px = parts["images"](images=[small], return_tensors="pt")["pixel_values"].to(dev)
                v = torch.nn.functional.normalize(features(torch, model.get_image_features(pixel_values=px)), dim=-1)[0]
        return v.cpu().numpy().astype(np.float16)
