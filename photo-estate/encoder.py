"""
encoder.py — the SigLIP 2 model as the photo app (`embed.py serve`) holds it: its text side, for typed
searches (browse.py), and with `images` its image side too, for photos arriving from the phone (admin.py).

  Encoder(model_id, threads, images)   .start() loads it in the background, .load() in this thread
  .state                               its core.lifecycle.Lifecycle: loading, then ready or failed
  .wait(timeout)                       return once loaded; raise ModelLoading (503) or ModelFailed (500)
  .text(q)                             a query's unit vector (waits for the model, up to .patience seconds)
  .image(im)                           a PIL image's float16 vector, as `embed.py run` makes it
  features(torch, out)                 the vector in what a model call returns (embed.py uses it too)

Importing PyTorch and loading the model is most of the app's startup time, and only typed queries need
it, so it loads while the page, filters and thumbnails are already being served. A search that comes
first waits for it, but only WAIT seconds (it holds the catalog gate): then ModelLoading, which the app
answers 503. A load that raises ends the wait too: every later call raises ModelFailed with the error,
which the app answers 500 (photo_app.PhotoApp.handle; estate_mcp.py makes either one a tool error).
"""
from __future__ import annotations

import threading
import time

from core.lifecycle import Lifecycle, LoadFailed, StillLoading

WAIT = 90.0   # seconds a search waits for the model to finish loading (a client gives up at about 120)


class ModelLoading(StillLoading):
    pass


class ModelFailed(LoadFailed):
    pass


def features(torch, out):
    return out if torch.is_tensor(out) else out.pooler_output


class Encoder:
    def __init__(self, model_id: str, threads: int, images: bool = False):
        self.model_id, self.threads, self.want_images = model_id, threads, images
        self.parts = {}                      # torch, model, tok, and images (the image processor)
        self.state, self.lock = Lifecycle(f"the search model ({model_id})"), threading.Lock()   # one forward pass at a time
        self.patience = WAIT   # how long text(), side and image() wait for a load in flight

    def start(self) -> None:
        self.state.begin()   # here, not in the thread: a request from now on waits instead of finding it idle
        threading.Thread(target=self._load, daemon=True).start()

    def load(self) -> None:
        self.state.begin()
        self._load()

    def _open(self) -> dict:
        import torch   # pyright: ignore[reportMissingImports]  (the ml group: not in the dev environment)
        from transformers import AutoImageProcessor, AutoModel, AutoTokenizer   # pyright: ignore[reportMissingImports]
        torch.set_num_threads(self.threads)
        parts = dict(torch=torch, model=AutoModel.from_pretrained(self.model_id, dtype=torch.float32).eval(),
                     tok=AutoTokenizer.from_pretrained(self.model_id))
        if self.want_images:   # the image side, for photos arriving from the phone
            parts["images"] = AutoImageProcessor.from_pretrained(self.model_id)
        return parts

    def _load(self) -> None:
        t = time.time()
        try:
            self.parts.update(self._open())
        except BaseException as e:   # a missing model, no PyTorch, out of memory: every wait ends, with the error
            self.state.fail(e)
            print(f"text encoder failed to load: {self.state.snapshot().error}", flush=True)
            return
        self.state.succeed()
        try:
            self.text("warm-up")  # the first forward pass is ~1 s slower; take it here, not on a user's query
        except Exception as e:
            print(f"text encoder warm-up: {type(e).__name__}: {e}", flush=True)
        print(f"text encoder ready in {time.time() - t:.1f}s", flush=True)

    def wait(self, timeout: float | None) -> None:
        """Return once the model is loaded: ModelLoading if it isn't by `timeout` seconds (None: until the
        load ends; an encoder never started raises at once), ModelFailed if it failed to load."""
        self.state.require(timeout, ModelLoading, ModelFailed)

    def text(self, q: str):
        import numpy as np
        # average the raw query with a "a photo of ..." prompt: SigLIP was trained on captions
        prompts = [q.lower(), f"a photo of {q.lower()}"]
        self.wait(self.patience)
        torch, model = self.parts["torch"], self.parts["model"]
        with self.lock, torch.no_grad():
            ti = self.parts["tok"](prompts, padding="max_length", max_length=64, return_tensors="pt")
            v = torch.nn.functional.normalize(features(torch, model.get_text_features(**ti)), dim=-1).mean(0)
        v = v.numpy()
        return v / np.linalg.norm(v)

    @property
    def side(self) -> int:
        """The image side's input size, in pixels (waits for the model)."""
        self.wait(self.patience)
        return self.parts["images"].size["height"]

    def image(self, small):
        """The vector for a photo already squashed to side x side, as `embed.py run` computes it."""
        import numpy as np
        self.wait(self.patience)
        torch, model = self.parts["torch"], self.parts["model"]
        with self.lock, torch.no_grad():
            px = self.parts["images"](images=[small], return_tensors="pt")["pixel_values"]
            v = torch.nn.functional.normalize(features(torch, model.get_image_features(pixel_values=px)), dim=-1)[0]
        return v.numpy().astype(np.float16)
